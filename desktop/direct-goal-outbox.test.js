"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  DirectGoalOutbox, MAX_ADMISSION_BYTES, MAX_RECORDS, stableJson,
} = require("./direct-goal-outbox");

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-direct-goal-outbox-"));
  const userData = path.join(root, "Nexus user data");
  const projectA = path.join(root, "arbitrary project A");
  const projectB = path.join(root, "another project B");
  fs.mkdirSync(projectA, {recursive: true});
  fs.mkdirSync(projectB, {recursive: true});
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  let moment = 0;
  const now = () => new Date(Date.UTC(2026, 8, 1, 10, 0, moment++)).toISOString();
  return {root, userData, projectA, projectB, now};
}

function payload(overrides = {}) {
  return {
    project_id: "board-project-a",
    lead_id: "lead-agent-a",
    chat_id: "chat-a",
    text: "Implement the exact portable goal.",
    attachments: [],
    ...overrides,
  };
}

function record(overrides = {}) {
  const exactPayload = payload(overrides.payload || {});
  return {
    schema_version: 1,
    chat_id: exactPayload.chat_id,
    request_id: "request-a",
    intent: "",
    payload: exactPayload,
    ...Object.fromEntries(Object.entries(overrides).filter(([key]) => key !== "payload")),
  };
}

function oneStore(held, projectPath = held.projectA) {
  return new DirectGoalOutbox({
    userDataPath: held.userData, projectPath, now: held.now,
  });
}

function dataAttachment(bytes, name = "exact.bin", type = "application/octet-stream") {
  return {
    name, type, size: bytes.length,
    data: `data:${type};base64,${bytes.toString("base64")}`,
  };
}

test("save is atomic, restart-persistent, and public inventory omits exact payload bytes", (t) => {
  const held = fixture(t);
  const store = oneStore(held);
  const bytes = Buffer.from([0, 1, 2, 127, 128, 254, 255]);
  const exact = record({payload: {
    text: "A Unicode goal — exact after restart 🚀",
    attachments: [dataAttachment(bytes)],
  }});
  const saved = store.save(exact);

  assert.equal(saved.schema_version, 1);
  assert.equal(saved.chat_id, "chat-a");
  assert.equal(saved.request_id, "request-a");
  assert.equal(saved.intent, saved.payload_sha256);
  assert.equal(saved.project_id, "board-project-a");
  assert.equal(saved.lead_id, "lead-agent-a");
  assert.equal(saved.attachment_count, 1);
  assert.equal(saved.attachment_bytes, bytes.length);
  assert.equal(saved.text_preview, exact.payload.text);
  assert.equal(Object.hasOwn(saved, "payload"), false);
  assert.equal(JSON.stringify(saved).includes(bytes.toString("base64")), false);
  assert.deepEqual(
    fs.readdirSync(held.userData).filter((name) => name.endsWith(".part")), [],
  );

  const restarted = oneStore(held);
  assert.deepEqual(restarted.list(), [saved]);
  const reread = restarted.read("chat-a", "request-a", saved.payload_sha256);
  assert.deepEqual(reread.payload, exact.payload);
  assert.deepEqual(
    Buffer.from(reread.payload.attachments[0].data.split(",")[1], "base64"), bytes,
  );
});

test("payload digest matches Python long_horizon_intent_sha256 canonical JSON", (t) => {
  const held = fixture(t);
  const bytes = Buffer.from([0x00, 0x7f, 0x80, 0xff]);
  const exact = record({payload: {
    project_id: "prøject",
    lead_id: "lead-α",
    chat_id: "chat-🧪",
    text: "Keep Unicode unescaped: 雪",
    attachments: [dataAttachment(bytes, "雪.bin")],
  }, chat_id: "chat-🧪"});
  const saved = oneStore(held).save(exact);
  const a = exact.payload.attachments[0];
  const canonical = `{"attachments":[{"data":${JSON.stringify(a.data)},"name":${JSON.stringify(a.name)},"size":4,"type":"application/octet-stream"}],"chat_id":"chat-🧪","lead_id":"lead-α","project_id":"prøject","schema_version":1,"text":"Keep Unicode unescaped: 雪"}`;
  const expected = crypto.createHash("sha256").update(canonical, "utf8").digest("hex");
  assert.equal(saved.payload_sha256, expected);
  assert.equal(saved.intent, expected);
});

test("same chat is idempotent only for the same request and exact payload", (t) => {
  const held = fixture(t);
  const store = oneStore(held);
  const first = record();
  const saved = store.save(first);
  assert.deepEqual(store.save(first), saved);

  assert.throws(
    () => store.save(record({request_id: "request-b"})),
    (error) => error.code === "NEXUS_OUTBOX_PENDING" && /Reconcile or discard/.test(error.message),
  );
  assert.throws(
    () => store.save(record({payload: {chat_id: "chat-b"}, chat_id: "chat-b"})),
    (error) => error.code === "NEXUS_OUTBOX_MISMATCH",
  );
  assert.throws(
    () => store.read("chat-a", "request-a", "0".repeat(64)),
    (error) => error.code === "NEXUS_OUTBOX_MISMATCH",
  );
  assert.deepEqual(store.list(), [saved]);
});

test("compare-and-delete never removes a missing, changed, or replacement request", (t) => {
  const held = fixture(t);
  const store = oneStore(held);
  const first = store.save(record());
  assert.deepEqual(
    store.delete("chat-a", "request-a", "0".repeat(64)),
    {deleted: false, reason: "mismatch"},
  );
  assert.equal(store.list().length, 1);
  assert.deepEqual(
    store.delete("chat-a", "request-a", first.payload_sha256),
    {deleted: true, reason: "deleted"},
  );
  assert.deepEqual(
    store.delete("chat-a", "request-a", first.payload_sha256),
    {deleted: false, reason: "missing"},
  );

  const replacement = store.save(record({request_id: "request-replacement", payload: {
    text: "Replacement exact payload",
  }}));
  assert.deepEqual(
    store.delete("chat-a", "request-a", first.payload_sha256),
    {deleted: false, reason: "mismatch"},
  );
  assert.equal(store.list()[0].payload_sha256, replacement.payload_sha256);
});

test("integrity tampering and malformed JSON fail closed without overwriting evidence", (t) => {
  const held = fixture(t);
  const store = oneStore(held);
  store.save(record());
  const envelope = JSON.parse(fs.readFileSync(store.file, "utf8"));
  envelope.records[0].payload.text = "tampered without metadata";
  fs.writeFileSync(store.file, JSON.stringify(envelope), "utf8");
  const tampered = fs.readFileSync(store.file, "utf8");
  assert.throws(() => store.list(), /integrity check failed/);
  assert.throws(() => store.save(record({request_id: "request-b"})), /integrity check failed/);
  assert.equal(fs.readFileSync(store.file, "utf8"), tampered);

  // Even a matching unsigned envelope checksum cannot hide per-record payload
  // metadata corruption.
  const resigned = JSON.parse(tampered);
  const unsigned = {
    format: resigned.format, version: resigned.version,
    revision: resigned.revision, records: resigned.records,
  };
  resigned.integrity.digest = crypto.createHash("sha256")
    .update(stableJson(unsigned), "utf8").digest("hex");
  fs.writeFileSync(store.file, JSON.stringify(resigned), "utf8");
  assert.throws(() => store.list(), /exact payload metadata/);

  fs.writeFileSync(store.file, "{not-json", "utf8");
  assert.throws(() => store.delete("chat-a", "request-a", "0".repeat(64)), /invalid JSON/);
  assert.equal(fs.readFileSync(store.file, "utf8"), "{not-json");
});

test("the outbox enforces record, text, attachment, and admission bounds", (t) => {
  assert.equal(MAX_ADMISSION_BYTES, 12_000_000);
  assert.equal(MAX_RECORDS, 20);
  const held = fixture(t);
  const store = oneStore(held);
  for (let index = 0; index < MAX_RECORDS; index += 1) {
    store.save(record({
      chat_id: `chat-${index}`,
      request_id: `request-${index}`,
      payload: {chat_id: `chat-${index}`, text: `Goal ${index}`},
    }));
  }
  assert.equal(store.list().length, MAX_RECORDS);
  assert.throws(
    () => store.save(record({chat_id: "chat-over", request_id: "request-over", payload: {
      chat_id: "chat-over", text: "One too many",
    }})),
    (error) => error.code === "NEXUS_OUTBOX_FULL",
  );

  const otherHeld = fixture(t);
  const other = oneStore(otherHeld);
  assert.throws(
    () => other.save(record({payload: {text: "x".repeat(200_001)}})),
    /1 to 200000 characters/,
  );
  assert.throws(
    () => other.save(record({payload: {
      attachments: [{name: "empty", type: "application/octet-stream", size: 0,
        data: "data:application/octet-stream;base64,"}],
    }})),
    /integer from 1/,
  );
  assert.throws(
    () => other.save(record({payload: {
      attachments: [{name: "bad", type: "application/octet-stream", size: 2,
        data: "data:application/octet-stream;base64,AA=="}],
    }})),
    /do not match/,
  );
  assert.throws(
    () => other.save(record({payload: {
      attachments: Array.from({length: 7}, (_one, index) => (
        dataAttachment(Buffer.from([index]), `${index}.bin`)
      )),
    }})),
    /at most 6 attachments/,
  );
});

test("the exact eight-megabyte attachment boundary is accepted and one byte more is refused", (t) => {
  const held = fixture(t);
  const fourMegabytes = Buffer.alloc(4_000_000, 0xa5);
  const exactAttachments = [
    dataAttachment(fourMegabytes, "one.bin"),
    dataAttachment(fourMegabytes, "two.bin"),
  ];
  const store = oneStore(held);
  const saved = store.save(record({payload: {attachments: exactAttachments}}));
  assert.equal(saved.attachment_bytes, 8_000_000);
  assert.ok(fs.statSync(store.file).size < MAX_ADMISSION_BYTES);

  const another = oneStore(fixture(t));
  assert.throws(
    () => another.save(record({payload: {attachments: [
      ...exactAttachments, dataAttachment(Buffer.from([1]), "one-more.bin"),
    ]}})),
    (error) => error.code === "NEXUS_OUTBOX_TOO_LARGE",
  );
});

test("project fingerprint files isolate inventory and do not let a full old project block a new one", (t) => {
  const held = fixture(t);
  const first = oneStore(held, held.projectA);
  for (let index = 0; index < MAX_RECORDS; index += 1) {
    first.save(record({
      chat_id: `chat-${index}`, request_id: `request-${index}`,
      payload: {chat_id: `chat-${index}`, text: `Project A goal ${index}`},
    }));
  }
  const second = oneStore(held, held.projectB);
  assert.deepEqual(second.list(), []);
  assert.equal(second.read("chat-0", "request-0", first.list()[0].payload_sha256), null);
  const savedB = second.save(record());
  assert.equal(second.list().length, 1);
  assert.equal(first.list().length, MAX_RECORDS);
  assert.notEqual(first.projectFingerprint, second.projectFingerprint);
  assert.notEqual(first.file, second.file);
  assert.equal(savedB.project_fingerprint, second.projectFingerprint);
});

test("a project alias has one canonical fingerprint while user-data reparse points fail closed", (t) => {
  const held = fixture(t);
  const projectAlias = path.join(held.root, "project alias");
  const userDataReal = path.join(held.root, "real user data");
  const userDataAlias = path.join(held.root, "user data alias");
  fs.mkdirSync(userDataReal, {recursive: true});
  try {
    fs.symlinkSync(held.projectA, projectAlias, process.platform === "win32" ? "junction" : "dir");
    fs.symlinkSync(userDataReal, userDataAlias, process.platform === "win32" ? "junction" : "dir");
  } catch (error) {
    if (["EPERM", "EACCES", "ENOTSUP"].includes(error.code)) {
      t.skip(`This filesystem cannot create a test reparse point: ${error.code}`);
      return;
    }
    throw error;
  }
  const direct = oneStore(held, held.projectA);
  const viaAlias = oneStore(held, projectAlias);
  assert.equal(viaAlias.projectFingerprint, direct.projectFingerprint);
  assert.equal(viaAlias.file, direct.file);
  assert.throws(
    () => new DirectGoalOutbox({userDataPath: userDataAlias, projectPath: held.projectA}),
    /reparse point|resolves outside/,
  );
});

test("preload exposes only bounded named IPC methods and the packaged main owns every handler", () => {
  const preload = fs.readFileSync(path.join(__dirname, "preload.js"), "utf8");
  const main = fs.readFileSync(path.join(__dirname, "main.js"), "utf8");
  const packageJson = JSON.parse(fs.readFileSync(path.join(__dirname, "package.json"), "utf8"));
  for (const name of ["save", "list", "read", "delete"]) {
    const publicName = `${name}DirectGoalOutbox`;
    const channel = `harness:${publicName}`;
    assert.match(preload, new RegExp(`${publicName}:[\\s\\S]*${channel}`));
    assert.ok(main.includes(`ipcMain.handle("${channel}"`));
  }
  assert.ok(packageJson.build.files.includes("direct-goal-outbox.js"));
  assert.doesNotMatch(preload, /node:fs|readFileSync|writeFileSync|realpathSync|app\.getPath/);
});
