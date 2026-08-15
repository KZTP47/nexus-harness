"use strict";

// Everything about starting, watching, and stopping the local harness server.
// It is kept apart from the window code so it can be tested on its own.

const { spawn } = require("node:child_process");
const os = require("node:os");

const READY_MARKER = "harness-ui-ready ";
const START_TIMEOUT_MS = 45000;
const LOG_LINES = 200;

// The first command that answers is the one we use. A user can name their own
// with HARNESS_PYTHON when they keep Python somewhere unusual.
function pythonCandidates(environment = process.env) {
  const named = String(environment.HARNESS_PYTHON || "").trim();
  // When someone names their own Python, use that one and no other. A silent
  // fall back to a different interpreter would be very confusing.
  if (named) return [[named, []]];
  const found = [];
  if (os.platform() === "win32") {
    found.push(["py", ["-3"]], ["python", []], ["python3", []]);
  } else {
    found.push(["python3", []], ["python", []]);
  }
  return found;
}

// The server prints one line that names the address it really bound to. Reading
// it is how the window finds the port when the system chose a free one.
function readReadyLine(text) {
  for (const line of String(text).split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed.startsWith(READY_MARKER)) continue;
    try {
      const value = JSON.parse(trimmed.slice(READY_MARKER.length));
      if (typeof value.url === "string" && value.url.startsWith("http://")) return value;
    } catch (error) {
      return null;
    }
  }
  return null;
}

function isLoopbackUrl(candidate) {
  let parsed;
  try {
    parsed = new URL(candidate);
  } catch (error) {
    return false;
  }
  if (parsed.protocol !== "http:") return false;
  const host = parsed.hostname.replace(/^\[|\]$/g, "");
  return host === "127.0.0.1" || host === "localhost" || host === "::1";
}

class HarnessServer {
  constructor(options = {}) {
    this.spawnProcess = options.spawn || spawn;
    this.candidates = options.candidates || pythonCandidates();
    this.timeoutMs = options.timeoutMs || START_TIMEOUT_MS;
    this.child = null;
    this.url = "";
    this.log = [];
    this.onExit = options.onExit || (() => {});
  }

  remember(line) {
    this.log.push(line);
    if (this.log.length > LOG_LINES) this.log.shift();
  }

  recentLog() {
    return this.log.join("\n");
  }

  // Try each Python command in turn. The first one that prints the ready line
  // wins; the rest are only tried when the command itself is missing.
  async start(projectPath) {
    let lastProblem = "No Python command was found.";
    for (const [command, leadingArguments] of this.candidates) {
      try {
        return await this.startOnce(command, leadingArguments, projectPath);
      } catch (error) {
        lastProblem = error.message;
      }
    }
    throw new Error(lastProblem);
  }

  startOnce(command, leadingArguments, projectPath) {
    const argv = [
      ...leadingArguments,
      "-m", "our_harness",
      "--project", projectPath,
      "ui", "--port", "0", "--no-open-browser",
    ];
    return new Promise((resolve, reject) => {
      let child;
      try {
        child = this.spawnProcess(command, argv, { cwd: projectPath, windowsHide: true });
      } catch (error) {
        reject(new Error(`Could not start ${command}: ${error.message}`));
        return;
      }
      let settled = false;
      let buffered = "";
      const finish = (error, value) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (error) {
          try { child.kill(); } catch (ignored) { /* already gone */ }
          reject(error);
        } else {
          resolve(value);
        }
      };
      const timer = setTimeout(
        () => finish(new Error(`${command} did not open the control panel within ${Math.round(this.timeoutMs / 1000)} seconds.`)),
        this.timeoutMs
      );
      const readStream = (stream) => {
        if (!stream) return;
        stream.setEncoding("utf8");
        stream.on("data", (chunk) => {
          this.remember(chunk.trimEnd());
          if (settled) return;
          buffered += chunk;
          const ready = readReadyLine(buffered);
          if (!ready) return;
          if (!isLoopbackUrl(ready.url)) {
            finish(new Error("The control panel reported an address that is not on this machine."));
            return;
          }
          this.child = child;
          this.url = ready.url;
          finish(null, ready.url);
        });
      };
      readStream(child.stdout);
      readStream(child.stderr);
      child.on("error", (error) => finish(new Error(`Could not start ${command}: ${error.message}`)));
      child.on("exit", (code) => {
        const detail = this.recentLog().split("\n").slice(-6).join("\n");
        finish(new Error(`${command} stopped with code ${code}.\n${detail}`));
        if (settled && this.child === child) {
          this.child = null;
          this.url = "";
          this.onExit(code);
        }
      });
    });
  }

  stop() {
    const child = this.child;
    this.child = null;
    this.url = "";
    if (!child) return;
    try {
      child.kill();
    } catch (error) {
      /* the process was already gone */
    }
  }
}

module.exports = { HarnessServer, pythonCandidates, readReadyLine, isLoopbackUrl, READY_MARKER };
