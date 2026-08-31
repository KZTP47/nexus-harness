"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const SETTINGS_FORMAT = "nexus-desktop-settings";
const SETTINGS_VERSION = 1;
const INTEGRITY_ALGORITHM = "sha256";

function jsonClone(value, label = "Nexus desktop settings") {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} must be an object`);
  }
  try {
    return JSON.parse(JSON.stringify(value));
  } catch (error) {
    throw new Error(`${label} must contain JSON-compatible values. ${error.message || error}`);
  }
}

function stableJson(value) {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  return `{${Object.keys(value).sort().map(
    (key) => `${JSON.stringify(key)}:${stableJson(value[key])}`
  ).join(",")}}`;
}

function sha256(value) {
  return crypto.createHash("sha256").update(String(value), "utf8").digest("hex");
}

function cleanRecoveredWebChats(value) {
  return (Array.isArray(value) ? value : [])
    .filter((one) => one && typeof one === "object" && !Array.isArray(one))
    .map((one) => jsonClone(one, "A recovered web-chat binding"));
}

function cleanRecovery(value) {
  if (value === null || value === undefined) return null;
  if (!value || typeof value !== "object" || Array.isArray(value)
      || value.kind !== "desktop_settings_recovery") {
    throw new Error("The desktop settings recovery record is not valid");
  }
  return {
    kind: "desktop_settings_recovery",
    id: String(value.id || "").slice(0, 128),
    reason: String(value.reason || "copies_disagree").slice(0, 80),
    selectedSource: value.selectedSource === "backup" ? "backup" : "primary",
    backupWon: Boolean(value.backupWon),
    copiesDisagreed: Boolean(value.copiesDisagreed),
    detectedRevision: Math.max(0, Number(value.detectedRevision) || 0),
    recoveredWebChats: cleanRecoveredWebChats(value.recoveredWebChats),
  };
}

function createSettingsEnvelope(payload, options = {}) {
  const revision = Number(options.revision);
  if (!Number.isSafeInteger(revision) || revision < 1) {
    throw new Error("A desktop settings revision must be a positive safe integer");
  }
  const unsigned = {
    format: SETTINGS_FORMAT,
    version: SETTINGS_VERSION,
    revision,
    payload: jsonClone(payload),
    recovery: cleanRecovery(options.recovery),
  };
  return {
    ...unsigned,
    integrity: {
      algorithm: INTEGRITY_ALGORITHM,
      digest: sha256(stableJson(unsigned)),
    },
  };
}

function inspectParsedSettings(value, source) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return {source, state: "invalid", issue: "not_an_object"};
  }
  if (value.format !== SETTINGS_FORMAT) {
    const payload = jsonClone(value);
    return {
      source, state: "valid", legacy: true, revision: 0, payload, recovery: null,
      digest: sha256(stableJson({legacy: payload})),
    };
  }
  if (Number.isSafeInteger(value.version) && value.version > SETTINGS_VERSION) {
    // A prior app cannot validate or migrate a future envelope contract. Keep
    // it distinguishable from corruption so no routine write can downgrade it.
    return {
      source, state: "unsupported", issue: "newer_format_version",
      version: value.version,
      ...(Number.isSafeInteger(value.revision) && value.revision > 0
        ? {revision: value.revision} : {}),
    };
  }
  try {
    if (value.version !== SETTINGS_VERSION) throw new Error("unsupported_version");
    if (!Number.isSafeInteger(value.revision) || value.revision < 1) {
      throw new Error("invalid_revision");
    }
    if (value.integrity?.algorithm !== INTEGRITY_ALGORITHM
        || !/^[a-f0-9]{64}$/.test(String(value.integrity?.digest || ""))) {
      throw new Error("invalid_integrity_record");
    }
    const unsigned = {
      format: SETTINGS_FORMAT,
      version: SETTINGS_VERSION,
      revision: value.revision,
      payload: jsonClone(value.payload),
      recovery: cleanRecovery(value.recovery),
    };
    const digest = sha256(stableJson(unsigned));
    if (digest !== value.integrity.digest) throw new Error("integrity_mismatch");
    return {
      source, state: "valid", legacy: false, revision: value.revision,
      payload: unsigned.payload, recovery: unsigned.recovery, digest,
    };
  } catch (error) {
    return {source, state: "invalid", issue: String(error?.message || error)};
  }
}

function inspectSettingsFile(file, source, fileSystem = fs) {
  try {
    const parsed = JSON.parse(fileSystem.readFileSync(file, "utf8"));
    return inspectParsedSettings(parsed, source);
  } catch (error) {
    if (error?.code === "ENOENT") return {source, state: "missing", issue: "missing"};
    return {source, state: "invalid", issue: "invalid_json_or_unreadable"};
  }
}

function chooseNewest(primary, backup) {
  const valid = [primary, backup].filter((one) => one.state === "valid");
  valid.sort((left, right) => right.revision - left.revision
    || (left.source === "primary" ? -1 : 1));
  return valid[0] || null;
}

function copiesDisagree(primary, backup) {
  if (primary.state === "missing" && backup.state === "missing") return false;
  // A lone legacy primary is the supported one-time migration input, not a
  // recovery incident. Its first ordinary write creates both envelopes.
  if (primary.state === "valid" && primary.legacy && backup.state === "missing") return false;
  if (primary.state !== "valid" || backup.state !== "valid") return true;
  return primary.digest !== backup.digest;
}

function recoveryReason(primary, backup, selected) {
  if (!selected) {
    if (primary.state === "invalid" && backup.state === "invalid") {
      return "both_copies_invalid";
    }
    if (primary.state === "invalid" && backup.state === "missing") {
      return "primary_invalid_backup_missing";
    }
    if (primary.state === "missing" && backup.state === "invalid") {
      return "primary_missing_backup_invalid";
    }
    return "no_valid_copy";
  }
  if (selected?.source === "backup") {
    if (primary.state === "missing") return "primary_missing";
    if (primary.state === "invalid") return "primary_invalid";
    if (backup.revision > primary.revision) return "backup_newer";
    return "backup_selected";
  }
  if (backup.state === "missing") return "backup_missing";
  if (backup.state === "invalid") return "backup_invalid";
  if (primary.state === "valid" && backup.state === "valid") {
    if (primary.revision > backup.revision) return "primary_newer";
    return "same_revision_disagreement";
  }
  return "copies_disagree";
}

function mergeWebChats(recovered, active) {
  const merged = new Map();
  const unkeyed = [];
  for (const one of [...cleanRecoveredWebChats(recovered), ...cleanRecoveredWebChats(active)]) {
    const id = String(one.id || "").toLowerCase();
    if (id) merged.set(id, one);
    else unkeyed.push(one);
  }
  return [...merged.values(), ...unkeyed];
}

function withoutRecoveredWebChats(payload, recovery) {
  const clean = jsonClone(payload);
  const recoveredIds = new Set(cleanRecoveredWebChats(recovery?.recoveredWebChats)
    .map((one) => String(one.id || "").toLowerCase()).filter(Boolean));
  if (Array.isArray(clean.webChats)) {
    clean.webChats = clean.webChats.filter(
      (one) => !recoveredIds.has(String(one?.id || "").toLowerCase()));
    if (!clean.webChats.length) delete clean.webChats;
  }
  return clean;
}

function copyStatus(one) {
  return {
    state: one.state,
    ...(one.state === "valid" ? {revision: one.revision, legacy: Boolean(one.legacy)} : {}),
    ...(one.state === "unsupported" ? {
      version: one.version, ...(one.revision ? {revision: one.revision} : {}),
    } : {}),
    ...(one.issue ? {issue: one.issue} : {}),
  };
}

function updateRequiredError(status) {
  const found = Array.isArray(status?.found_format_versions)
    ? status.found_format_versions.join(", ") : "newer";
  const error = new Error(
    `These desktop settings use newer format version ${found}; this Nexus app supports version ${SETTINGS_VERSION}. `
    + "Nexus left both settings copies untouched. Open them with the newer Nexus version."
  );
  error.code = "NEXUS_SETTINGS_UPDATE_REQUIRED";
  return error;
}

function writeTextAtomically(file, text, fileSystem = fs) {
  const temporary = `${file}.${process.pid}-${crypto.randomBytes(6).toString("hex")}.part`;
  let descriptor = null;
  try {
    descriptor = fileSystem.openSync(temporary, "wx");
    fileSystem.writeFileSync(descriptor, text, "utf8");
    fileSystem.fsyncSync(descriptor);
    fileSystem.closeSync(descriptor);
    descriptor = null;
    fileSystem.renameSync(temporary, file);
    // fsyncing the file protects its bytes; on POSIX the rename itself is not
    // power-loss durable until the containing directory is synced too. Windows
    // may reject directory descriptors, so this remains a bounded best effort
    // on platforms where the filesystem does not expose that operation.
    let directoryDescriptor = null;
    try {
      directoryDescriptor = fileSystem.openSync(path.dirname(file), "r");
      fileSystem.fsyncSync(directoryDescriptor);
    } catch (_error) {
      /* the mirrored copy still provides the recovery boundary */
    } finally {
      if (directoryDescriptor !== null) {
        try { fileSystem.closeSync(directoryDescriptor); } catch (_error) { /* best effort */ }
      }
    }
  } finally {
    if (descriptor !== null) {
      try { fileSystem.closeSync(descriptor); } catch (_error) { /* best effort */ }
    }
    try { fileSystem.unlinkSync(temporary); } catch (_error) { /* renamed or never made */ }
  }
}

class DesktopSettingsStore {
  constructor(options = {}) {
    this.primaryFile = String(options.primaryFile || "");
    this.backupFile = String(options.backupFile || "");
    if (!this.primaryFile || !this.backupFile || this.primaryFile === this.backupFile) {
      throw new Error("Desktop settings need distinct primary and backup files");
    }
    this.fileSystem = options.fileSystem || fs;
    this.atomicWriter = options.atomicWriter || ((file, text) => (
      writeTextAtomically(file, text, this.fileSystem)
    ));
  }

  snapshot() {
    const primary = inspectSettingsFile(this.primaryFile, "primary", this.fileSystem);
    const backup = inspectSettingsFile(this.backupFile, "backup", this.fileSystem);
    const selected = chooseNewest(primary, backup);
    const currentDisagreement = copiesDisagree(primary, backup);
    const foundFormatVersions = [...new Set([primary, backup]
      .filter((one) => one.state === "unsupported")
      .map((one) => one.version))].sort((left, right) => left - right);
    const updateRequired = foundFormatVersions.length > 0;
    const backupWon = selected?.source === "backup";
    const equalRevisionConflict = primary.state === "valid" && backup.state === "valid"
      && primary.revision === backup.revision && primary.digest !== backup.digest;
    let settings = selected ? jsonClone(selected.payload) : {};
    let recovery = selected?.recovery ? cleanRecovery(selected.recovery) : null;

    // Missing both files is a normal first launch. Any other state with no
    // readable copy is different: silently treating it as fresh settings lets
    // the next routine write erase the incident before the renderer can tell
    // the user. Keep a zero-chat recovery choice so an explicit Repair action
    // creates a new matching pair and the warning survives ordinary writes.
    if (!updateRequired && !selected && currentDisagreement) {
      const reason = recoveryReason(primary, backup, selected);
      recovery = {
        kind: "desktop_settings_recovery",
        id: sha256(stableJson({
          reason, primary: copyStatus(primary), backup: copyStatus(backup),
        })).slice(0, 32),
        reason,
        selectedSource: "primary",
        backupWon: false,
        copiesDisagreed: true,
        detectedRevision: 0,
        recoveredWebChats: [],
      };
    }
    if (!updateRequired && selected && (backupWon || currentDisagreement)) {
      // A newer primary is the documented commit point after a crash between
      // mirror writes. Equal revisions with different integrity digests have no
      // such ordering evidence, so routes from the deterministic tie winner are
      // quarantined just like routes recovered from a winning backup.
      const newlyRecovered = backupWon || equalRevisionConflict
        ? cleanRecoveredWebChats(settings.webChats) : [];
      const recoveredWebChats = mergeWebChats(
        recovery?.recoveredWebChats || [], newlyRecovered);
      const reason = recovery?.reason || recoveryReason(primary, backup, selected);
      recovery = {
        kind: "desktop_settings_recovery",
        id: recovery?.id || sha256(stableJson({
          reason, selected: selected.digest, recoveredWebChats,
        })).slice(0, 32),
        reason,
        selectedSource: recovery?.selectedSource || selected.source,
        backupWon: Boolean(recovery?.backupWon || backupWon),
        copiesDisagreed: Boolean(recovery?.copiesDisagreed || currentDisagreement),
        detectedRevision: Math.max(recovery?.detectedRevision || 0, selected.revision),
        recoveredWebChats,
      };
    }
    if (updateRequired) {
      // A current-format peer is not authoritative beside an unreadable future
      // copy. Other usable preferences can help the app open, but provider
      // routes fail closed and neither file may be rewritten by this version.
      delete settings.webChats;
      recovery = null;
    } else if (recovery) settings = withoutRecoveredWebChats(settings, recovery);
    const maxRevision = Math.max(
      primary.state === "valid" ? primary.revision : 0,
      backup.state === "valid" ? backup.revision : 0,
    );
    const recoveredCount = recovery?.recoveredWebChats.length || 0;
    const status = {
      state: updateRequired ? "update_required" : recovery ? "recovery_pending"
        : currentDisagreement ? "copies_disagree" : "ok",
      format_version: SETTINGS_VERSION,
      selected_source: selected?.source || "none",
      selected_revision: selected?.revision || 0,
      primary: copyStatus(primary),
      backup: copyStatus(backup),
      backup_won: Boolean(recovery?.backupWon || backupWon),
      copies_disagree: Boolean(recovery?.copiesDisagreed || currentDisagreement),
      copies_currently_disagree: currentDisagreement,
      reason: updateRequired ? "newer_format_version" : recovery?.reason || (currentDisagreement
        ? recoveryReason(primary, backup, selected) : ""),
      resolution_required: !updateRequired && Boolean(recovery),
      requires_web_chat_resolution: !updateRequired && recoveredCount > 0,
      recovered_web_chat_count: updateRequired ? 0 : recoveredCount,
      legacy_migration: Boolean(selected?.legacy),
      update_required: updateRequired,
      write_blocked: updateRequired,
      supported_format_version: SETTINGS_VERSION,
      found_format_versions: foundFormatVersions,
    };
    return {primary, backup, selected, settings, recovery, status, maxRevision};
  }

  read() {
    return this.snapshot().settings;
  }

  status() {
    return this.snapshot().status;
  }

  commit(payload, recovery, revision) {
    const envelope = createSettingsEnvelope(payload, {revision, recovery});
    const text = `${JSON.stringify(envelope, null, 2)}\n`;
    this.fileSystem.mkdirSync(path.dirname(this.primaryFile), {recursive: true});
    this.fileSystem.mkdirSync(path.dirname(this.backupFile), {recursive: true});
    let previousPrimary = null;
    try {
      previousPrimary = {
        existed: true, text: this.fileSystem.readFileSync(this.primaryFile, "utf8"),
      };
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
      previousPrimary = {existed: false, text: ""};
    }
    // The primary is the authoritative commit point; the backup mirrors that
    // exact integrity envelope. A crash between them deterministically picks
    // the newer valid primary rather than resurrecting stale backup data.
    this.atomicWriter(this.primaryFile, text);
    try {
      this.atomicWriter(this.backupFile, text);
    } catch (error) {
      // Runtime write failures (as opposed to a power loss) can still be made
      // transaction-like: the backup writer is itself atomic, so restore the
      // primary to the exact pre-commit bytes before reporting the failure.
      try {
        if (previousPrimary.existed) {
          this.atomicWriter(this.primaryFile, previousPrimary.text);
        } else {
          try { this.fileSystem.unlinkSync(this.primaryFile); } catch (removeError) {
            if (removeError?.code !== "ENOENT") throw removeError;
          }
        }
      } catch (rollbackError) {
        throw new Error(
          `${error.message || error} Nexus also could not roll back the primary settings copy: ${rollbackError.message || rollbackError}`);
      }
      throw error;
    }
    return this.read();
  }

  write(value) {
    const snapshot = this.snapshot();
    if (snapshot.status.update_required) throw updateRequiredError(snapshot.status);
    let payload = jsonClone(value);
    // Ordinary settings writes are not recovery consent. Keep the protected
    // routes out of the active payload and carry the recovery record forward.
    if (snapshot.recovery) payload = withoutRecoveredWebChats(payload, snapshot.recovery);
    return this.commit(payload, snapshot.recovery, snapshot.maxRevision + 1);
  }

  resolve(action) {
    if (!new Set(["restore", "discard_web_chats"]).has(action)) {
      throw new Error("Choose restore or discard_web_chats for desktop settings recovery");
    }
    const snapshot = this.snapshot();
    if (snapshot.status.update_required) throw updateRequiredError(snapshot.status);
    if (!snapshot.recovery) return {status: snapshot.status, changed: false};
    const payload = jsonClone(snapshot.settings);
    if (action === "restore") {
      payload.webChats = mergeWebChats(
        snapshot.recovery.recoveredWebChats, payload.webChats || []);
      if (!payload.webChats.length) delete payload.webChats;
    }
    this.commit(payload, null, snapshot.maxRevision + 1);
    return {status: this.status(), changed: true};
  }
}

module.exports = {
  SETTINGS_FORMAT, SETTINGS_VERSION, DesktopSettingsStore,
  createSettingsEnvelope, inspectParsedSettings, writeTextAtomically,
};
