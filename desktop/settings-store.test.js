"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  DesktopSettingsStore, createSettingsEnvelope, writeTextAtomically,
} = require("./settings-store");
const {WebChatManager, WebChatTurnError} = require("./web-chats");

function temporaryStore(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-settings-store-"));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const primaryFile = path.join(root, "settings.json");
  const backupFile = path.join(root, "settings.last-good.json");
  return {
    root, primaryFile, backupFile,
    store: new DesktopSettingsStore({primaryFile, backupFile}),
  };
}

function writeJson(file, value) {
  fs.writeFileSync(file, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

function recoveredGemini(id = "gemini-recovered") {
  return {
    id, provider: "gemini", title: "Recovered Gemini",
    url: "https://gemini.google.com/app/recovered-portable-chat",
  };
}

test("a newer valid backup beats a shortened but valid primary deterministically", (t) => {
  const {store, primaryFile, backupFile} = temporaryStore(t);
  writeJson(primaryFile, createSettingsEnvelope(
    {lastProject: "short-primary"}, {revision: 4}));
  writeJson(backupFile, createSettingsEnvelope({
    lastProject: "newer-backup", keptPreference: true,
    webChats: [recoveredGemini()],
  }, {revision: 7}));

  const settings = store.read();
  const status = store.status();

  assert.equal(settings.lastProject, "newer-backup");
  assert.equal(settings.keptPreference, true);
  assert.equal(settings.webChats, undefined);
  assert.equal(status.selected_source, "backup");
  assert.equal(status.selected_revision, 7);
  assert.equal(status.backup_won, true);
  assert.equal(status.copies_disagree, true);
  assert.equal(status.reason, "backup_newer");
  assert.equal(status.requires_web_chat_resolution, true);
  assert.equal(status.recovered_web_chat_count, 1);
});

test("same-revision split-brain copies never activate the selected routes silently", (t) => {
  const {store, primaryFile, backupFile} = temporaryStore(t);
  writeJson(primaryFile, createSettingsEnvelope({
    lastProject: "primary-choice", webChats: [recoveredGemini("gemini-primary")],
  }, {revision: 6}));
  writeJson(backupFile, createSettingsEnvelope({
    lastProject: "backup-choice", webChats: [recoveredGemini("gemini-backup")],
  }, {revision: 6}));

  assert.equal(store.read().lastProject, "primary-choice");
  assert.equal(store.read().webChats, undefined);
  const pending = store.status();
  assert.equal(pending.reason, "same_revision_disagreement");
  assert.equal(pending.selected_source, "primary");
  assert.equal(pending.requires_web_chat_resolution, true);
  assert.equal(pending.recovered_web_chat_count, 1);

  store.resolve("restore");
  assert.deepEqual(store.read().webChats.map((one) => one.id), ["gemini-primary"]);
});

test("a corrupt primary recovers other settings but quarantines backup web chats", (t) => {
  const {store, primaryFile, backupFile} = temporaryStore(t);
  fs.writeFileSync(primaryFile, "{partially-written", "utf8");
  writeJson(backupFile, createSettingsEnvelope({
    lastProject: "still-usable", webChats: [recoveredGemini()],
  }, {revision: 3}));

  assert.equal(store.read().lastProject, "still-usable");
  assert.equal(store.read().webChats, undefined);
  assert.deepEqual(store.status().primary, {
    state: "invalid", issue: "invalid_json_or_unreadable",
  });
  assert.equal(store.status().reason, "primary_invalid");
  assert.equal(store.status().backup_won, true);
});

test("no readable settings copy stays visible until explicit repair", (t) => {
  const {store, primaryFile, backupFile} = temporaryStore(t);
  fs.writeFileSync(primaryFile, "{partially-written", "utf8");
  fs.writeFileSync(backupFile, "not-json-either", "utf8");

  const pending = store.status();
  assert.equal(pending.state, "recovery_pending");
  assert.equal(pending.selected_source, "none");
  assert.equal(pending.reason, "both_copies_invalid");
  assert.equal(pending.resolution_required, true);
  assert.equal(pending.requires_web_chat_resolution, false);
  assert.equal(pending.recovered_web_chat_count, 0);

  // An ordinary startup preference write must carry the warning forward rather
  // than making the corruption incident disappear before the UI can show it.
  store.write({lastProject: "portable-project"});
  assert.equal(store.status().state, "recovery_pending");
  assert.equal(store.read().lastProject, "portable-project");

  const outcome = store.resolve("restore");
  assert.equal(outcome.changed, true);
  assert.equal(store.status().state, "ok");
  const primary = JSON.parse(fs.readFileSync(primaryFile, "utf8"));
  const backup = JSON.parse(fs.readFileSync(backupFile, "utf8"));
  assert.equal(primary.integrity.digest, backup.integrity.digest);
  assert.equal(primary.payload.lastProject, "portable-project");
});

test("a prior app never overwrites or activates a newer settings format", (t) => {
  const {store, primaryFile, backupFile} = temporaryStore(t);
  const future = JSON.stringify({
    format: "nexus-desktop-settings", version: 2, revision: 14,
    payload: {webChats: [recoveredGemini("future-gemini")]},
    recovery: null, integrity: {algorithm: "sha256", digest: "0".repeat(64)},
  });
  fs.writeFileSync(primaryFile, future, "utf8");
  fs.writeFileSync(backupFile, future, "utf8");

  const status = store.status();
  assert.equal(status.state, "update_required");
  assert.equal(status.update_required, true);
  assert.equal(status.write_blocked, true);
  assert.deepEqual(status.found_format_versions, [2]);
  assert.equal(status.resolution_required, false);
  assert.equal(store.read().webChats, undefined);
  assert.throws(() => store.write({lastProject: "must-not-downgrade"}),
    /newer format version 2/);
  assert.throws(() => store.resolve("restore"), /left both settings copies untouched/);
  assert.equal(fs.readFileSync(primaryFile, "utf8"), future);
  assert.equal(fs.readFileSync(backupFile, "utf8"), future);
});

test("a future peer also makes current-format routes fail closed", (t) => {
  const {store, primaryFile, backupFile} = temporaryStore(t);
  writeJson(primaryFile, createSettingsEnvelope({
    lastProject: "usable-without-routes", webChats: [recoveredGemini("current-route")],
  }, {revision: 8}));
  fs.writeFileSync(backupFile, JSON.stringify({
    format: "nexus-desktop-settings", version: 3, revision: 9,
  }), "utf8");

  assert.equal(store.read().lastProject, "usable-without-routes");
  assert.equal(store.read().webChats, undefined);
  assert.equal(store.status().state, "update_required");
  assert.throws(() => store.write(store.read()), /newer format version 3/);
});

test("valid JSON with a broken integrity digest cannot outrank the backup", (t) => {
  const {store, primaryFile, backupFile} = temporaryStore(t);
  const tampered = createSettingsEnvelope({lastProject: "original"}, {revision: 20});
  tampered.payload.lastProject = "tampered-after-signing";
  writeJson(primaryFile, tampered);
  writeJson(backupFile, createSettingsEnvelope({
    lastProject: "verified-backup", webChats: [recoveredGemini()],
  }, {revision: 3}));

  assert.equal(store.read().lastProject, "verified-backup");
  assert.equal(store.read().webChats, undefined);
  assert.equal(store.status().primary.issue, "integrity_mismatch");
  assert.equal(store.status().selected_source, "backup");
});

test("a recovered route is neither advertised nor dispatchable before confirmation", async (t) => {
  const {store, primaryFile, backupFile} = temporaryStore(t);
  fs.writeFileSync(primaryFile, "not-json", "utf8");
  writeJson(backupFile, createSettingsEnvelope({
    webChats: [recoveredGemini()],
  }, {revision: 2}));
  const manager = new WebChatManager({
    electron: {}, owner: null,
    readSettings: () => store.read(), writeSettings: (value) => store.write(value),
    shellPage: "file:///web-chat.html", shellPreload: "web-chat-shell-preload.js",
  });

  assert.deepEqual(manager.list(), []);
  await assert.rejects(
    () => manager.askNow("gemini-recovered", "Do not dispatch", []),
    (error) => error instanceof WebChatTurnError
      && error.deliveryState === "not_accepted"
      && error.failureCode === "connection_missing",
  );
});

test("restore atomically activates recovered routes and manager reload publishes them", (t) => {
  const {store, primaryFile, backupFile} = temporaryStore(t);
  writeJson(primaryFile, createSettingsEnvelope(
    {lastProject: "older"}, {revision: 2}));
  writeJson(backupFile, createSettingsEnvelope({
    lastProject: "recovered", webChats: [recoveredGemini()],
  }, {revision: 5}));
  const changed = [];
  const manager = new WebChatManager({
    electron: {}, owner: {
      isDestroyed: () => false,
      webContents: {send: (...args) => changed.push(args)},
    },
    readSettings: () => store.read(), writeSettings: (value) => store.write(value),
    shellPage: "file:///web-chat.html", shellPreload: "web-chat-shell-preload.js",
  });
  assert.deepEqual(manager.list(), []);

  assert.deepEqual(store.resolve("restore"), {
    status: store.status(), changed: true,
  });
  const routes = manager.reloadFromSettings();

  assert.deepEqual(routes.map((one) => one.id), ["gemini-recovered"]);
  assert.deepEqual(changed.at(-1)[1].map((one) => one.id), ["gemini-recovered"]);
  assert.equal(changed.at(-1)[2], null);
  assert.equal(store.status().state, "ok");
  const primary = JSON.parse(fs.readFileSync(primaryFile, "utf8"));
  const backup = JSON.parse(fs.readFileSync(backupFile, "utf8"));
  assert.equal(primary.integrity.digest, backup.integrity.digest);
  assert.equal(primary.revision, backup.revision);

  const restarted = new DesktopSettingsStore({primaryFile, backupFile});
  assert.deepEqual(restarted.read().webChats.map((one) => one.id), ["gemini-recovered"]);
  assert.equal(restarted.status().resolution_required, false);
});

test("ordinary writes preserve quarantine across restart and discard drops only recovered chats", (t) => {
  const {store, primaryFile, backupFile} = temporaryStore(t);
  fs.writeFileSync(primaryFile, "{broken", "utf8");
  writeJson(backupFile, createSettingsEnvelope({
    lastProject: "before", webChats: [recoveredGemini()],
  }, {revision: 8}));

  const activeChat = {
    id: "chatgpt-new-active", provider: "chatgpt", title: "New active chat",
    url: "https://chatgpt.com/c/new-active-chat",
  };
  store.write({
    ...store.read(), lastProject: "after-ordinary-write", webChats: [activeChat],
  });
  const restarted = new DesktopSettingsStore({primaryFile, backupFile});

  assert.equal(restarted.read().lastProject, "after-ordinary-write");
  assert.deepEqual(restarted.read().webChats.map((one) => one.id), ["chatgpt-new-active"]);
  assert.equal(restarted.status().state, "recovery_pending");
  assert.equal(restarted.status().recovered_web_chat_count, 1);

  const outcome = restarted.resolve("discard_web_chats");
  assert.equal(outcome.changed, true);
  assert.equal(restarted.read().lastProject, "after-ordinary-write");
  assert.deepEqual(restarted.read().webChats.map((one) => one.id), ["chatgpt-new-active"]);
  assert.equal(restarted.status().state, "ok");

  const afterSecondRestart = new DesktopSettingsStore({primaryFile, backupFile});
  assert.equal(afterSecondRestart.read().lastProject, "after-ordinary-write");
  assert.deepEqual(afterSecondRestart.read().webChats.map((one) => one.id),
    ["chatgpt-new-active"]);
  assert.equal(afterSecondRestart.status().resolution_required, false);
});

test("legacy settings migrate once into matching integrity envelopes", (t) => {
  const {store, primaryFile, backupFile} = temporaryStore(t);
  writeJson(primaryFile, {lastProject: "legacy", webChats: []});

  assert.equal(store.status().legacy_migration, true);
  store.write({...store.read(), lastProjectAt: "portable-time"});

  const primary = JSON.parse(fs.readFileSync(primaryFile, "utf8"));
  const backup = JSON.parse(fs.readFileSync(backupFile, "utf8"));
  assert.equal(primary.format, "nexus-desktop-settings");
  assert.equal(primary.version, 1);
  assert.equal(primary.revision, 1);
  assert.equal(primary.integrity.digest, backup.integrity.digest);
  assert.equal(store.status().legacy_migration, false);
});

test("recovery resolution rejects unrecognized renderer choices", (t) => {
  const {store} = temporaryStore(t);
  assert.throws(() => store.resolve("restore_everything_else"),
    /restore or discard_web_chats/);
});

test("a mirror write failure rolls the primary back and remains visible", (t) => {
  const {root, primaryFile, backupFile} = temporaryStore(t);
  writeJson(primaryFile, {lastProject: "before"});
  let failBackupOnce = true;
  const store = new DesktopSettingsStore({
    primaryFile, backupFile,
    atomicWriter: (file, text) => {
      if (file === backupFile && failBackupOnce) {
        failBackupOnce = false;
        throw new Error("backup volume unavailable");
      }
      writeTextAtomically(file, text);
    },
  });

  assert.throws(
    () => store.write({lastProject: "must-not-partially-commit", root}),
    /backup volume unavailable/,
  );
  assert.deepEqual(JSON.parse(fs.readFileSync(primaryFile, "utf8")), {
    lastProject: "before",
  });
  assert.equal(fs.existsSync(backupFile), false);
  assert.equal(store.read().lastProject, "before");
});

test("the sandboxed renderer gets only bounded recovery status and resolution APIs", () => {
  const main = fs.readFileSync(path.join(__dirname, "main.js"), "utf8");
  const preload = fs.readFileSync(path.join(__dirname, "preload.js"), "utf8");
  const packageJson = JSON.parse(
    fs.readFileSync(path.join(__dirname, "package.json"), "utf8"));

  assert.match(main, /ipcMain\.handle\("harness:desktopSettingsRecoveryStatus"/);
  assert.match(main, /ipcMain\.handle\("harness:resolveDesktopSettingsRecovery"/);
  assert.match(main, /webChatManager\.reloadFromSettings\(\)/);
  assert.match(main, /reload_error:/);
  assert.match(main, /restored_connection_count:/);
  assert.match(main, /settingsStore\(\)\.status\(\)\.write_blocked/);
  assert.match(preload, /desktopSettingsRecoveryStatus:.*ipcRenderer\.invoke\(/s);
  assert.match(preload, /resolveDesktopSettingsRecovery:.*ipcRenderer\.invoke\(/s);
  assert.ok(packageJson.build.files.includes("settings-store.js"));
});
