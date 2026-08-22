"use strict";

// Everything about starting, watching, and stopping the local harness server.
// It is kept apart from the window code so it can be tested on its own.

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

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

// Where the harness's own code lives, when it has not been installed into
// Python.
//
// The code sits in a src folder beside this app, which is the ordinary shape of
// a Python project and is not somewhere Python looks by itself. Started without
// this, every Python on the machine answered "No module named our_harness", and
// the app showed three of those and nothing anybody could act on. Everybody who
// has only downloaded the project - which is everybody, the first time - hit it.
function whereTheHarnessLives(
  appFolder = __dirname,
  projectPath = "",
  resources = "",
  options = {}
) {
  const looking = [];
  const projectSource = projectPath ? path.resolve(projectPath, "src") : "";
  // The installed app normally wins, because its Python and desktop halves
  // were released together. After a confirmed schema-version mismatch, the
  // user can explicitly ask to use the newer harness source in the project.
  // This is the same code `python scripts/harness.py ui` would use, without
  // making them leave the error screen and type the command themselves.
  if (options.preferProjectHarness && projectSource) looking.push(projectSource);
  // An installed app has no src folder beside it - it has a resources folder,
  // and the harness is put in there when the app is built. Without this the
  // installed app was an empty window: it could only ever work if the project
  // somebody picked happened to be a copy of the harness itself.
  const carried = resources || process.resourcesPath || "";
  if (carried) looking.push(path.resolve(carried, "harness", "src"));
  looking.push(path.resolve(appFolder, "..", "src"));
  if (projectSource && !options.preferProjectHarness) looking.push(projectSource);
  return looking.filter((one) => {
    try {
      return fs.existsSync(path.join(one, "our_harness", "__init__.py"));
    } catch (error) {
      return false;
    }
  });
}

// What the harness is started with. Its own code goes on the path in front of
// whatever is already there, so a plain download works with nothing installed.
// Python is told about it only when the files are really there, so nobody who
// has installed it properly is sent somewhere else.
function environmentForStarting(environment = process.env, folders = []) {
  const started = { ...environment };
  if (!folders.length) return started;
  const already = String(environment.PYTHONPATH || "").trim();
  started.PYTHONPATH = [...folders, ...(already ? [already] : [])].join(path.delimiter);
  return started;
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

// The window may show this machine's harness, or one of the app's own pages.
// Both sides are decoded before comparing, because a folder name with a space
// in it arrives as %20 and would otherwise never match.
function isOwnPage(candidate, pagesFolderUrl) {
  let parsed;
  try {
    parsed = new URL(candidate);
  } catch (error) {
    return false;
  }
  if (parsed.protocol !== "file:") return false;
  let folder;
  try {
    folder = decodeURIComponent(String(pagesFolderUrl));
  } catch (error) {
    return false;
  }
  let target;
  try {
    target = decodeURIComponent(parsed.href);
  } catch (error) {
    return false;
  }
  if (!folder.endsWith("/")) folder += "/";
  return target.startsWith(folder) && !target.slice(folder.length).includes("..");
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

function isWebAddress(candidate) {
  // Only an ordinary web address may be handed to the system to open. A file
  // path, a network share, a mail link or any other kind of address would let
  // whatever the window is showing start something on this machine.
  let parsed;
  try {
    parsed = new URL(candidate);
  } catch (error) {
    return false;
  }
  return parsed.protocol === "http:" || parsed.protocol === "https:";
}

class HarnessServer {
  constructor(options = {}) {
    this.spawnProcess = options.spawn || spawn;
    this.environment = options.environment || process.env;
    this.appFolder = options.appFolder || __dirname;
    this.resources = options.resources || process.resourcesPath || "";
    this.candidates = options.candidates || pythonCandidates(this.environment);
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

  // Try each Python command in turn until one prints the ready line. A machine
  // often has several, and only one of them has the harness installed, so all
  // of them are tried.
  //
  // What is said when none works is chosen with care. Reporting whichever was
  // tried last points at a command the person never chose: "python3 is not
  // here" when the real answer is "py ran, and the harness is not installed in
  // it". So the failure from a Python that really ran is kept and reported,
  // and "not on this machine" is only the answer when that was true of all of
  // them.
  async start(projectPath, options = {}) {
    const missing = [];
    let realProblem = null;
    for (const [command, leadingArguments] of this.candidates) {
      try {
        return await this.startOnce(command, leadingArguments, projectPath, options);
      } catch (error) {
        if (error.commandIsMissing) missing.push(command);
        else if (!realProblem) realProblem = error;
      }
    }
    if (realProblem) throw realProblem;
    throw new Error(
      `No Python was found on this machine. Tried: ${missing.join(", ")}. `
      + "Install Python 3.11 or newer, or name yours with HARNESS_PYTHON."
    );
  }

  startOnce(command, leadingArguments, projectPath, options = {}) {
    const argv = [
      ...leadingArguments,
      "-m", "our_harness",
      "--project", projectPath,
      "ui", "--port", "0", "--no-open-browser",
    ];
    return new Promise((resolve, reject) => {
      // A command that is not on this machine is not a failure worth showing:
      // it only means try the next one. Anything else is the real answer.
      const notHere = (error) => {
        const problem = new Error(`Could not start ${command}: ${error.message}`);
        problem.commandIsMissing = error.code === "ENOENT" || error.code === "EACCES";
        return problem;
      };
      let child;
      try {
        child = this.spawnProcess(command, argv, {
          cwd: projectPath,
          env: environmentForStarting(
            this.environment,
            whereTheHarnessLives(this.appFolder, projectPath, this.resources, options)
          ),
          windowsHide: true,
        });
      } catch (error) {
        reject(notHere(error));
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
      child.on("error", (error) => finish(notHere(error)));
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

module.exports = { HarnessServer, whereTheHarnessLives, environmentForStarting, pythonCandidates, readReadyLine, isLoopbackUrl, isWebAddress, isOwnPage, READY_MARKER };
