"use strict";

const fs = require("node:fs");
const path = require("node:path");

// electron-builder 26.15.3 signs extraResource EXEs while copying them.
// signExts is an endsWith list, not a glob list. Preserve exactly the Java
// executables in the verified runtime manifest, in source and packaged paths.
function withPreservedKestraJava(configured, desktop = __dirname) {
  const manifest = JSON.parse(fs.readFileSync(path.join(desktop, "kestra-runtime", "NEXUS_KESTRA.json"), "utf8"));
  if (manifest.schema_version !== 1 || !manifest.files || typeof manifest.files !== "object") {
    throw new Error("The bundled Kestra manifest is missing its signing boundary");
  }
  const suffixes = Object.keys(manifest.files).filter(name => /^java\/.+\.exe$/.test(name)).map(name => {
    if (name.split("/").some(part => !part || part === ".." || part === ".") || !/^[A-Za-z0-9_./-]+$/.test(name)) {
      throw new Error("Invalid Java executable path in runtime manifest");
    }
    return `/kestra-runtime/${name}`;
  });
  if (!suffixes.some(name => name.endsWith("/java/bin/java.exe"))) {
    throw new Error("The bundled Java executable is missing from its manifest");
  }
  const existing = configured.win?.signExts || [];
  for (const rule of existing) {
    // Builder applies positive suffixes before exclusions. Reject conflicts.
    if (!rule.startsWith("!") && suffixes.some(name => name.endsWith(rule) || name.replaceAll("/", "\\").endsWith(rule))) {
      throw new Error("An explicit signing rule would modify the pinned Java runtime");
    }
  }
  return {
    ...configured,
    win: {
      ...configured.win,
      signExts: [...existing, ...suffixes.flatMap(name => [`!${name}`, `!${name.replaceAll("/", "\\")}`])],
    },
  };
}

module.exports = { withPreservedKestraJava };
