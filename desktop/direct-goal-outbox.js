"use strict";

// A bounded, machine-local pre-network outbox for direct long-horizon goal
// requests.  It deliberately has no transport code: reading a record is an
// explicit renderer action, and only the Python admission journal can decide
// whether that payload should subsequently be admitted.

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const OUTBOX_FORMAT = "nexus-direct-goal-outbox";
const OUTBOX_VERSION = 1;
const OUTBOX_FILENAME_PREFIX = "direct-goal-outbox-v1-";
const MAX_RECORDS = 20;
const MAX_ADMISSION_BYTES = 12_000_000;
const MAX_ATTACHMENTS = 6;
const MAX_ATTACHMENT_BYTES = 8_000_000;
const MAX_ONE_ATTACHMENT_BYTES = 4_000_000;
const MAX_TEXT_CHARACTERS = 200_000;

function fail(code, message) {
  const error = new Error(message);
  error.code = code;
  return error;
}

function stableJson(value) {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  return `{${Object.keys(value).sort().map(
    (key) => `${JSON.stringify(key)}:${stableJson(value[key])}`
  ).join(",")}}`;
}

function sha256Text(value) {
  return crypto.createHash("sha256").update(String(value), "utf8").digest("hex");
}

function exactJsonClone(value, label) {
  try {
    const encoded = JSON.stringify(value);
    if (encoded === undefined) throw new Error("value is not represented by JSON");
    return JSON.parse(encoded);
  } catch (error) {
    throw fail("NEXUS_OUTBOX_INVALID", `${label} must contain exact JSON-compatible values. ${error.message || error}`);
  }
}

function hasUnpairedSurrogate(value) {
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    if (code >= 0xD800 && code <= 0xDBFF) {
      const next = value.charCodeAt(index + 1);
      if (!(next >= 0xDC00 && next <= 0xDFFF)) return true;
      index += 1;
    } else if (code >= 0xDC00 && code <= 0xDFFF) return true;
  }
  return false;
}

function boundedString(value, label, maximum, options = {}) {
  if (typeof value !== "string") throw fail("NEXUS_OUTBOX_INVALID", `${label} must be text.`);
  const characters = [...value].length;
  if ((!options.allowEmpty && !value) || characters > maximum) {
    throw fail(
      "NEXUS_OUTBOX_INVALID",
      `${label} must contain ${options.allowEmpty ? "0" : "1"} to ${maximum} characters.`,
    );
  }
  if (hasUnpairedSurrogate(value)) {
    throw fail("NEXUS_OUTBOX_INVALID", `${label} contains an invalid Unicode surrogate.`);
  }
  return value;
}

function exactKeys(value, allowed, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw fail("NEXUS_OUTBOX_INVALID", `${label} must be an object.`);
  }
  const unknown = Object.keys(value).filter((key) => !allowed.has(key));
  if (unknown.length) {
    throw fail("NEXUS_OUTBOX_INVALID", `${label} contains unsupported field ${unknown[0]}.`);
  }
}

function normalizedAttachment(raw, index) {
  exactKeys(raw, new Set(["name", "type", "size", "data"]), `Attachment ${index + 1}`);
  const name = boundedString(raw.name, `Attachment ${index + 1} name`, 180);
  const type = boundedString(raw.type, `Attachment ${index + 1} type`, 160);
  if (!Number.isSafeInteger(raw.size) || raw.size < 1 || raw.size > MAX_ONE_ATTACHMENT_BYTES) {
    throw fail(
      "NEXUS_OUTBOX_INVALID",
      `Attachment ${index + 1} size must be an integer from 1 to ${MAX_ONE_ATTACHMENT_BYTES}.`,
    );
  }
  const data = boundedString(
    raw.data, `Attachment ${index + 1} data`, 5_500_000,
  );
  const matched = /^data:([^,]{1,320});base64,([A-Za-z0-9+/]*={0,2})$/i.exec(data);
  if (!matched || matched[2].length % 4 !== 0) {
    throw fail("NEXUS_OUTBOX_INVALID", `Attachment ${index + 1} is not an exact base64 data URL.`);
  }
  const bytes = Buffer.from(matched[2], "base64");
  if (bytes.toString("base64") !== matched[2] || bytes.length !== raw.size) {
    throw fail(
      "NEXUS_OUTBOX_INVALID",
      `Attachment ${index + 1} bytes do not match its exact base64 data and size.`,
    );
  }
  return {attachment: {name, type, size: raw.size, data}, bytes: bytes.length};
}

function normalizedPayload(raw) {
  exactKeys(
    raw,
    new Set(["project_id", "lead_id", "chat_id", "text", "attachments"]),
    "A direct-goal payload",
  );
  const projectId = boundedString(raw.project_id, "Project ID", 512);
  const leadId = boundedString(raw.lead_id, "Lead agent ID", 256);
  const chatId = boundedString(raw.chat_id, "Chat ID", 256);
  const text = boundedString(raw.text, "Direct-goal text", MAX_TEXT_CHARACTERS);
  if (!text.trim()) {
    throw fail("NEXUS_OUTBOX_INVALID", "Direct-goal text must contain a visible character.");
  }
  if ([...text].some((letter) => letter.codePointAt(0) < 32 && !"\t\n\r".includes(letter))) {
    throw fail("NEXUS_OUTBOX_INVALID", "Direct-goal text contains a control character.");
  }
  if (!Array.isArray(raw.attachments) || raw.attachments.length > MAX_ATTACHMENTS) {
    throw fail("NEXUS_OUTBOX_INVALID", `Direct-goal work accepts at most ${MAX_ATTACHMENTS} attachments.`);
  }
  let attachmentBytes = 0;
  const attachments = raw.attachments.map((one, index) => {
    const normalized = normalizedAttachment(one, index);
    attachmentBytes += normalized.bytes;
    return normalized.attachment;
  });
  if (attachmentBytes > MAX_ATTACHMENT_BYTES) {
    throw fail(
      "NEXUS_OUTBOX_TOO_LARGE",
      `Direct-goal attachments may contain at most ${MAX_ATTACHMENT_BYTES} decoded bytes.`,
    );
  }
  return {
    payload: {project_id: projectId, lead_id: leadId, chat_id: chatId, text, attachments},
    attachmentBytes,
  };
}

function canonicalIntent(payload) {
  return {
    schema_version: 1,
    chat_id: payload.chat_id,
    project_id: payload.project_id,
    lead_id: payload.lead_id,
    text: payload.text,
    attachments: payload.attachments,
  };
}

function payloadSha256(payload) {
  // This is intentionally the same recursively key-sorted, compact UTF-8 JSON
  // contract as Python chat.long_horizon_intent_sha256.
  return sha256Text(stableJson(canonicalIntent(payload)));
}

function admissionBytes(payload, requestId) {
  // Mirror the direct-admission record's current fixed fields as well as its
  // duplicated objectives text.  A record accepted here will therefore fit
  // the Python journal's 12 MB boundary when the user explicitly continues it.
  return Buffer.byteLength(stableJson({
    schema_version: 1,
    request_id: requestId,
    project_id: payload.project_id,
    lead_id: payload.lead_id,
    chat_id: payload.chat_id,
    text: payload.text,
    objectives: [payload.text],
    success_criteria: null,
    policy: null,
    attachments: payload.attachments,
  }), "utf8");
}

function normalizedPathForComparison(value) {
  const normalized = path.normalize(path.resolve(value));
  return process.platform === "win32" ? normalized.toLowerCase() : normalized;
}

function directoryPathParts(value) {
  const parsed = path.parse(value);
  return {
    root: parsed.root,
    names: value.slice(parsed.root.length).split(path.sep).filter(Boolean),
  };
}

function assertRealDirectoryChain(resolved, label) {
  const parts = directoryPathParts(resolved);
  let current = parts.root;
  for (const name of parts.names) {
    current = path.join(current, name);
    let held;
    try { held = fs.lstatSync(current); } catch (error) {
      throw fail("NEXUS_OUTBOX_PATH", `${label} is unavailable. ${error.message || error}`);
    }
    // On Windows, lstat reports both symbolic links and directory junctions as
    // symbolic links.  Inspect every component rather than only the leaf so a
    // parent reparse point cannot redirect application-owned storage.
    if (!held.isDirectory() || held.isSymbolicLink()) {
      throw fail("NEXUS_OUTBOX_PATH", `${label} must use real directories, not a reparse point.`);
    }
  }
}

function sameWindowsDirectoryAliases(resolved, canonical) {
  const requested = directoryPathParts(resolved);
  const real = directoryPathParts(canonical);
  if (requested.root.toLowerCase() !== real.root.toLowerCase()
      || requested.names.length !== real.names.length) return false;

  let requestedPart = requested.root;
  let realPart = real.root;
  for (let index = 0; index < requested.names.length; index += 1) {
    requestedPart = path.join(requestedPart, requested.names[index]);
    realPart = path.join(realPart, real.names[index]);
    const requestedStat = fs.statSync(requestedPart, {bigint: true});
    const realStat = fs.statSync(realPart, {bigint: true});
    if (!requestedStat.isDirectory() || !realStat.isDirectory()
        || requestedStat.dev !== realStat.dev || requestedStat.ino !== realStat.ino) return false;
  }
  return true;
}

function canonicalDirectory(directory, label, options = {}) {
  if (typeof directory !== "string" || !directory.trim()) {
    throw fail("NEXUS_OUTBOX_PATH", `${label} is not configured.`);
  }
  const resolved = path.resolve(directory);
  if (!resolved || resolved === path.parse(resolved).root && !options.allowRoot) {
    throw fail("NEXUS_OUTBOX_PATH", `${label} is not a safe application-owned directory.`);
  }
  if (options.create) fs.mkdirSync(resolved, {recursive: true});
  assertRealDirectoryChain(resolved, label);
  const real = fs.realpathSync.native(resolved);
  const exactSpelling = normalizedPathForComparison(real) === normalizedPathForComparison(resolved);
  const exactWindowsAlias = !exactSpelling && process.platform === "win32"
    && sameWindowsDirectoryAliases(resolved, real);
  if (!exactSpelling && !exactWindowsAlias) {
    throw fail("NEXUS_OUTBOX_PATH", `${label} resolves outside its application-owned path.`);
  }
  return real;
}

function canonicalProjectFingerprint(projectDirectory) {
  if (typeof projectDirectory !== "string" || !projectDirectory.trim()) {
    throw fail("NEXUS_OUTBOX_PROJECT", "No current project is open.");
  }
  const resolved = path.resolve(projectDirectory);
  let held;
  try { held = fs.statSync(resolved); } catch (error) {
    throw fail("NEXUS_OUTBOX_PROJECT", `The current project is unavailable. ${error.message || error}`);
  }
  if (!held.isDirectory()) throw fail("NEXUS_OUTBOX_PROJECT", "The current project is not a directory.");
  const canonical = fs.realpathSync.native(resolved);
  return sha256Text(`nexus-canonical-project-v1\0${canonical}`);
}

function assertSafeOutboxFile(root, file, filename) {
  const relative = path.relative(root, file);
  if (relative !== filename || path.isAbsolute(relative)
      || !/^direct-goal-outbox-v1-[a-f0-9]{64}\.json$/.test(filename)) {
    throw fail("NEXUS_OUTBOX_PATH", "The direct-goal outbox path escaped application storage.");
  }
  try {
    const held = fs.lstatSync(file);
    if (!held.isFile() || held.isSymbolicLink()) {
      throw fail("NEXUS_OUTBOX_PATH", "The direct-goal outbox is not a regular application-owned file.");
    }
    const real = fs.realpathSync.native(file);
    if (normalizedPathForComparison(path.dirname(real)) !== normalizedPathForComparison(root)
        || path.basename(real) !== filename) {
      throw fail("NEXUS_OUTBOX_PATH", "The direct-goal outbox resolves outside application storage.");
    }
    return true;
  } catch (error) {
    if (error?.code === "ENOENT") return false;
    throw error;
  }
}

function publicMetadata(record) {
  return exactJsonClone({
    schema_version: record.schema_version,
    project_fingerprint: record.project_fingerprint,
    project_id: record.project_id,
    lead_id: record.lead_id,
    chat_id: record.chat_id,
    request_id: record.request_id,
    intent: record.intent,
    payload_sha256: record.payload_sha256,
    text_preview: record.text_preview,
    text_characters: record.text_characters,
    attachment_count: record.attachment_count,
    attachment_bytes: record.attachment_bytes,
    created_at: record.created_at,
    updated_at: record.updated_at,
  }, "Direct-goal outbox metadata");
}

function createRecord(raw, projectFingerprint, now) {
  exactKeys(
    raw,
    new Set(["schema_version", "chat_id", "request_id", "intent", "payload"]),
    "A direct-goal outbox record",
  );
  if (raw.schema_version !== 1) {
    throw fail("NEXUS_OUTBOX_INVALID", "The direct-goal outbox record schema must be version 1.");
  }
  const chatId = boundedString(raw.chat_id, "Chat ID", 256);
  const requestId = boundedString(raw.request_id, "Request ID", 160);
  if (!/^[A-Za-z0-9][A-Za-z0-9._:-]*$/.test(requestId)) {
    throw fail("NEXUS_OUTBOX_INVALID", "Request ID contains unsupported characters.");
  }
  const normalized = normalizedPayload(raw.payload);
  if (normalized.payload.chat_id !== chatId) {
    throw fail("NEXUS_OUTBOX_INVALID", "The outbox record and payload name different chats.");
  }
  const digest = payloadSha256(normalized.payload);
  const suppliedIntent = raw.intent === undefined || raw.intent === null
    ? "" : boundedString(raw.intent, "Direct-goal intent", 64, {allowEmpty: true});
  if (suppliedIntent && suppliedIntent !== digest) {
    throw fail("NEXUS_OUTBOX_MISMATCH", "The supplied direct-goal intent does not match the exact payload.");
  }
  const bytes = admissionBytes(normalized.payload, requestId);
  if (bytes > MAX_ADMISSION_BYTES) {
    throw fail(
      "NEXUS_OUTBOX_TOO_LARGE",
      `The exact direct-goal admission is ${bytes} UTF-8 bytes; the limit is ${MAX_ADMISSION_BYTES}.`,
    );
  }
  const timestamp = boundedString(now(), "Outbox timestamp", 64);
  if (!Number.isFinite(Date.parse(timestamp))) {
    throw fail("NEXUS_OUTBOX_INVALID", "The outbox clock did not return an ISO timestamp.");
  }
  return {
    schema_version: 1,
    project_fingerprint: projectFingerprint,
    project_id: normalized.payload.project_id,
    lead_id: normalized.payload.lead_id,
    chat_id: chatId,
    request_id: requestId,
    intent: digest,
    payload_sha256: digest,
    text_preview: [...normalized.payload.text].slice(0, 500).join(""),
    text_characters: [...normalized.payload.text].length,
    attachment_count: normalized.payload.attachments.length,
    attachment_bytes: normalized.attachmentBytes,
    created_at: timestamp,
    updated_at: timestamp,
    payload: normalized.payload,
  };
}

const STORED_KEYS = new Set([
  "schema_version", "project_fingerprint", "project_id", "lead_id", "chat_id",
  "request_id", "intent", "payload_sha256", "text_preview", "text_characters",
  "attachment_count", "attachment_bytes", "created_at", "updated_at", "payload",
]);

function validateStoredRecord(raw) {
  exactKeys(raw, STORED_KEYS, "A saved direct-goal outbox record");
  if (raw.schema_version !== 1 || !/^[a-f0-9]{64}$/.test(String(raw.project_fingerprint || ""))) {
    throw fail("NEXUS_OUTBOX_CORRUPT", "A saved direct-goal outbox record has an invalid schema or project binding.");
  }
  let rebuilt;
  try {
    rebuilt = createRecord({
      schema_version: 1,
      chat_id: raw.chat_id,
      request_id: raw.request_id,
      intent: raw.intent,
      payload: raw.payload,
    }, raw.project_fingerprint, () => raw.created_at);
  } catch (error) {
    throw fail(
      "NEXUS_OUTBOX_CORRUPT",
      `A saved direct-goal outbox record does not match its exact payload metadata. ${error.message || error}`,
    );
  }
  rebuilt.updated_at = boundedString(raw.updated_at, "Outbox update time", 64);
  if (!Number.isFinite(Date.parse(rebuilt.updated_at))) {
    throw fail("NEXUS_OUTBOX_CORRUPT", "A saved direct-goal outbox record has an invalid update time.");
  }
  const expected = {...rebuilt};
  if (stableJson(expected) !== stableJson(raw)) {
    throw fail("NEXUS_OUTBOX_CORRUPT", "A saved direct-goal outbox record does not match its exact payload metadata.");
  }
  return expected;
}

function createEnvelope(records, revision) {
  const unsigned = {
    format: OUTBOX_FORMAT,
    version: OUTBOX_VERSION,
    revision,
    records,
  };
  return {
    ...unsigned,
    integrity: {algorithm: "sha256", digest: sha256Text(stableJson(unsigned))},
  };
}

function parseEnvelope(text) {
  let value;
  try { value = JSON.parse(text); } catch (_error) {
    throw fail("NEXUS_OUTBOX_CORRUPT", "The direct-goal outbox contains invalid JSON and was left untouched.");
  }
  if (!value || typeof value !== "object" || Array.isArray(value)
      || value.format !== OUTBOX_FORMAT || value.version !== OUTBOX_VERSION
      || !Number.isSafeInteger(value.revision) || value.revision < 1
      || !Array.isArray(value.records) || value.records.length > MAX_RECORDS
      || value.integrity?.algorithm !== "sha256"
      || !/^[a-f0-9]{64}$/.test(String(value.integrity?.digest || ""))) {
    throw fail("NEXUS_OUTBOX_CORRUPT", "The direct-goal outbox schema is invalid and was left untouched.");
  }
  if (Object.keys(value).some(
    (key) => !new Set(["format", "version", "revision", "records", "integrity"]).has(key)
  ) || Object.keys(value.integrity).some(
    (key) => !new Set(["algorithm", "digest"]).has(key)
  )) {
    throw fail("NEXUS_OUTBOX_CORRUPT", "The direct-goal outbox has unsupported fields and was left untouched.");
  }
  const unsigned = {
    format: value.format, version: value.version, revision: value.revision,
    records: value.records,
  };
  if (sha256Text(stableJson(unsigned)) !== value.integrity.digest) {
    throw fail("NEXUS_OUTBOX_CORRUPT", "The direct-goal outbox integrity check failed and was left untouched.");
  }
  const records = value.records.map(validateStoredRecord);
  const identities = new Set();
  const requests = new Set();
  for (const record of records) {
    const identity = `${record.project_fingerprint}\0${record.chat_id}`;
    const request = `${record.project_fingerprint}\0${record.request_id}`;
    if (identities.has(identity) || requests.has(request)) {
      throw fail("NEXUS_OUTBOX_CORRUPT", "The direct-goal outbox contains duplicate record identities.");
    }
    identities.add(identity);
    requests.add(request);
  }
  return {revision: value.revision, records};
}

function writeAtomically(root, file, filename, text) {
  const temporary = path.join(
    root, `${filename}.${process.pid}-${crypto.randomBytes(8).toString("hex")}.part`,
  );
  const relative = path.relative(root, temporary);
  if (!relative || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
    throw fail("NEXUS_OUTBOX_PATH", "The temporary outbox path escaped application storage.");
  }
  let descriptor = null;
  try {
    descriptor = fs.openSync(temporary, "wx");
    fs.writeFileSync(descriptor, text, "utf8");
    fs.fsyncSync(descriptor);
    fs.closeSync(descriptor);
    descriptor = null;
    assertSafeOutboxFile(root, file, filename);
    fs.renameSync(temporary, file);
    let directoryDescriptor = null;
    try {
      directoryDescriptor = fs.openSync(root, "r");
      fs.fsyncSync(directoryDescriptor);
    } catch (_error) { /* Windows may not expose directory fsync. */ }
    finally {
      if (directoryDescriptor !== null) {
        try { fs.closeSync(directoryDescriptor); } catch (_error) { /* best effort */ }
      }
    }
  } finally {
    if (descriptor !== null) {
      try { fs.closeSync(descriptor); } catch (_error) { /* best effort */ }
    }
    try { fs.unlinkSync(temporary); } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
  }
}

class DirectGoalOutbox {
  constructor(options = {}) {
    this.root = canonicalDirectory(options.userDataPath, "Nexus user data", {create: true});
    this.projectFingerprint = canonicalProjectFingerprint(options.projectPath);
    this.filename = `${OUTBOX_FILENAME_PREFIX}${this.projectFingerprint}.json`;
    this.file = path.join(this.root, this.filename);
    this.now = typeof options.now === "function" ? options.now : () => new Date().toISOString();
  }

  snapshot() {
    if (!assertSafeOutboxFile(this.root, this.file, this.filename)) {
      return {revision: 0, records: []};
    }
    const size = fs.statSync(this.file).size;
    if (size < 1 || size > MAX_ADMISSION_BYTES * MAX_RECORDS + 1_000_000) {
      throw fail("NEXUS_OUTBOX_CORRUPT", "The direct-goal outbox has an unsafe file size and was left untouched.");
    }
    const parsed = parseEnvelope(fs.readFileSync(this.file, "utf8"));
    if (parsed.records.some((one) => one.project_fingerprint !== this.projectFingerprint)) {
      throw fail(
        "NEXUS_OUTBOX_CORRUPT",
        "The direct-goal outbox contains a record for another project and was left untouched.",
      );
    }
    return parsed;
  }

  commit(snapshot, records) {
    const envelope = createEnvelope(records, snapshot.revision + 1);
    writeAtomically(this.root, this.file, this.filename, `${JSON.stringify(envelope)}\n`);
  }

  save(raw) {
    const candidate = createRecord(
      exactJsonClone(raw, "A direct-goal outbox record"),
      this.projectFingerprint,
      this.now,
    );
    const snapshot = this.snapshot();
    const existing = snapshot.records.find((one) => (
      one.project_fingerprint === this.projectFingerprint && one.chat_id === candidate.chat_id
    ));
    if (existing) {
      if (existing.request_id === candidate.request_id
          && existing.payload_sha256 === candidate.payload_sha256) {
        return publicMetadata(existing);
      }
      throw fail(
        "NEXUS_OUTBOX_PENDING",
        "This exact chat already has a durable local goal request. Reconcile or discard it first.",
      );
    }
    if (snapshot.records.some((one) => (
      one.project_fingerprint === this.projectFingerprint
        && one.request_id === candidate.request_id
    ))) {
      throw fail("NEXUS_OUTBOX_MISMATCH", "That request ID is already bound to another chat in this project.");
    }
    if (snapshot.records.length >= MAX_RECORDS) {
      throw fail(
        "NEXUS_OUTBOX_FULL",
        `The direct-goal outbox already contains its maximum of ${MAX_RECORDS} records. Reconcile or discard one first.`,
      );
    }
    this.commit(snapshot, [...snapshot.records, candidate]);
    return publicMetadata(candidate);
  }

  list() {
    return this.snapshot().records
      .filter((one) => one.project_fingerprint === this.projectFingerprint)
      .sort((left, right) => left.created_at.localeCompare(right.created_at)
        || left.chat_id.localeCompare(right.chat_id))
      .map(publicMetadata);
  }

  read(chatId, requestId, digest) {
    const exactChat = boundedString(chatId, "Chat ID", 256);
    const exactRequest = boundedString(requestId, "Request ID", 160);
    const exactDigest = boundedString(digest, "Payload digest", 64);
    const found = this.snapshot().records.find((one) => (
      one.project_fingerprint === this.projectFingerprint && one.chat_id === exactChat
    ));
    if (!found) return null;
    if (found.request_id !== exactRequest || found.payload_sha256 !== exactDigest) {
      throw fail("NEXUS_OUTBOX_MISMATCH", "The saved goal request changed; Nexus did not reveal or resend it.");
    }
    return exactJsonClone({...publicMetadata(found), payload: found.payload}, "A direct-goal outbox record");
  }

  delete(chatId, requestId, digest) {
    const exactChat = boundedString(chatId, "Chat ID", 256);
    const exactRequest = boundedString(requestId, "Request ID", 160);
    const exactDigest = boundedString(digest, "Payload digest", 64);
    const snapshot = this.snapshot();
    const index = snapshot.records.findIndex((one) => (
      one.project_fingerprint === this.projectFingerprint && one.chat_id === exactChat
    ));
    if (index < 0) return {deleted: false, reason: "missing"};
    const found = snapshot.records[index];
    if (found.request_id !== exactRequest || found.payload_sha256 !== exactDigest) {
      return {deleted: false, reason: "mismatch"};
    }
    const kept = [...snapshot.records];
    kept.splice(index, 1);
    this.commit(snapshot, kept);
    return {deleted: true, reason: "deleted"};
  }
}

module.exports = {
  OUTBOX_FORMAT, OUTBOX_VERSION, OUTBOX_FILENAME_PREFIX, MAX_RECORDS,
  MAX_ADMISSION_BYTES, DirectGoalOutbox, canonicalProjectFingerprint,
  payloadSha256, stableJson,
};
