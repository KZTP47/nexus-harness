"use strict";

// Everything about starting, watching, and stopping the local harness server.
// It is kept apart from the window code so it can be tested on its own.

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const READY_MARKER = "harness-ui-ready ";
const START_TIMEOUT_MS = 45000;
const STOP_TIMEOUT_MS = 8000;
const LOG_LINES = 200;

function privateRuntimeMissingError() {
  return new Error(
    "This installed Nexus package is incomplete: resources/runtime/python.exe is missing or was removed before it could start. "
    + "Reinstall the same checksummed release. If the private runtime disappears again, ask your IT or security "
    + "administrator to allow Nexus Harness and its bundled resources/runtime/python.exe. "
    + "Do not install a separate system Python; that will not repair this package."
  );
}

function projectFolderIsUnavailable(projectPath) {
  try {
    const details = fs.statSync(projectPath);
    if (!details.isDirectory()) return true;
    fs.accessSync(projectPath, fs.constants.R_OK | fs.constants.X_OK);
    return false;
  } catch (error) {
    return true;
  }
}

function projectFolderUnavailableError(projectPath, code) {
  return new Error(
    `The selected project folder is no longer available at ${projectPath} (${code || "unavailable"}). `
    + "Reopen Nexus Harness and choose an existing folder you can access. If it is on a network, removable, "
    + "or managed drive, reconnect it or ask your IT administrator to restore access. "
    + "The bundled runtime is still present; installing another Python will not help."
  );
}

// The first command that answers is the one we use. A user can name their own
// with HARNESS_PYTHON when they keep Python somewhere unusual.
function pythonCandidates(environment = process.env, resources = "", bundledRequired = false) {
  if (bundledRequired && os.platform() === "win32") {
    const privateRuntime = resources ? path.resolve(resources, "runtime", "python.exe") : "";
    return privateRuntime && fs.existsSync(privateRuntime) ? [[privateRuntime, []]] : [];
  }
  const named = String(environment.HARNESS_PYTHON || "").trim();
  // When someone names their own Python, use that one and no other. A silent
  // fall back to a different interpreter would be very confusing.
  if (named) return [[named, []]];
  const found = [];
  if (os.platform() === "win32") {
    const bundled = resources ? path.resolve(resources, "runtime", "python.exe") : "";
    if (bundled && fs.existsSync(bundled)) return [[bundled, []]];
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
  if (!options.bundledRequired && options.preferProjectHarness && projectSource) looking.push(projectSource);
  // An installed app has no src folder beside it - it has a resources folder,
  // and the harness is put in there when the app is built. Without this the
  // installed app was an empty window: it could only ever work if the project
  // somebody picked happened to be a copy of the harness itself.
  const carried = resources || process.resourcesPath || "";
  if (carried) looking.push(path.resolve(carried, "harness", "src"));
  looking.push(path.resolve(appFolder, "..", "src"));
  if (!options.bundledRequired && projectSource && !options.preferProjectHarness) looking.push(projectSource);
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
    this.bundledRequired = Boolean(options.bundledRequired);
    this.candidates = options.candidates || pythonCandidates(
      this.environment, this.resources, this.bundledRequired);
    this.timeoutMs = options.timeoutMs || START_TIMEOUT_MS;
    this.child = null;
    // Own every UI child from spawn until the operating system reports it
    // closed.  The ready child is also exposed as `child`, but startup and
    // timed-out shutdown processes must remain stoppable too.
    this.children = new Set();
    this.stopPromise = null;
    // A stop request cancels the whole in-flight candidate search, not just the
    // child that happened to exist when stop() took its snapshot. Without this
    // fence, source mode could kill candidate A and then start candidate B from
    // the rejection handler after shutdown had already completed.
    this.stopGeneration = 0;
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
    if (this.stopPromise) await this.stopPromise;
    const stopGeneration = this.stopGeneration;
    if (this.children.size) {
      throw new Error(
        "The previous local Nexus server has not stopped yet. Wait for it to close before opening another project."
      );
    }
    if (!this.candidates.length && this.bundledRequired) {
      throw privateRuntimeMissingError();
    }
    const missing = [];
    let realProblem = null;
    for (const [command, leadingArguments] of this.candidates) {
      try {
        const url = await this.startOnce(command, leadingArguments, projectPath, options);
        if (this.stopGeneration !== stopGeneration) {
          throw new Error("The local Nexus server was stopped before startup completed.");
        }
        return url;
      } catch (error) {
        if (this.stopGeneration !== stopGeneration) throw error;
        if (error.commandIsMissing) {
          // A packaged candidate existed when the app inspected its resources,
          // but antivirus or an updater can remove it before spawn. Never turn
          // that race into source-mode advice about installing another Python.
          if (this.bundledRequired) throw privateRuntimeMissingError();
          missing.push(command);
        }
        else if (error.commandAccessDenied && this.bundledRequired) throw error;
        else if (!realProblem) realProblem = error;
      }
    }
    if (realProblem) throw realProblem;
    throw new Error(
      `No Python was found on this machine for supported source mode. Tried: ${missing.join(", ")}. `
      + "Install Python 3.11 or newer, or name yours with HARNESS_PYTHON. "
      + "The installed Nexus release uses its own private runtime instead."
    );
  }

  async trustProject(projectPath, options = {}) {
    if (!options.reviewedConfig || !/^[0-9a-f]{64}$/i.test(String(options.expectedSha256 || ""))) {
      throw new Error("Trust requires the exact reviewed config path and its SHA-256 digest.");
    }
    let last = null;
    for (const [command, leadingArguments] of this.candidates) {
      try {
        return await new Promise((resolve, reject) => {
          const child = this.spawnProcess(command, [
            ...leadingArguments, "-m", "our_harness", "--project", projectPath,
            "trust", "--yes", "--reviewed-config", options.reviewedConfig,
            "--expected-sha256", options.expectedSha256,
          ], {
            cwd: projectPath,
            env: environmentForStarting(
              this.environment,
              whereTheHarnessLives(this.appFolder, projectPath, this.resources, {
                ...options, bundledRequired: this.bundledRequired,
              })
            ),
            windowsHide: true,
          });
          let output = "";
          const remember = (chunk) => { output = (output + String(chunk)).slice(-64000); };
          child.stdout?.on("data", remember);
          child.stderr?.on("data", remember);
          child.once("error", reject);
          child.once("exit", (code) => {
            if (code === 0) resolve(output.trim() || "Trusted.");
            else reject(new Error(output.trim() || `${command} stopped with code ${code}`));
          });
        });
      } catch (error) { last = error; }
    }
    throw last || new Error("No supported Nexus runtime was available to record trust.");
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
        const code = String(error?.code || "").toUpperCase();
        const denied = code === "EACCES" || code === "EPERM"
          || Number(error?.winerror) === 5
          || /(?:access is denied|permission denied)/i.test(String(error?.message || ""));
        // Windows uses the same spawn errors for a missing executable and a
        // missing/inaccessible cwd. If the private runtime is still present,
        // attribute the failure to the selected project instead of telling the
        // user to repair a runtime that is not actually missing.
        const projectUnavailable = this.bundledRequired
          && fs.existsSync(command)
          && projectFolderIsUnavailable(projectPath);
        const problem = projectUnavailable
          ? projectFolderUnavailableError(projectPath, code)
          : new Error(denied && this.bundledRequired
            ? `Windows refused access while starting the installed Nexus private runtime at ${command} `
              + `for the selected project folder at ${projectPath} (${code || "access denied"}). `
              + "Security policy or antivirus may have blocked or quarantined the runtime, or policy may deny "
              + "the project folder as a child-process working directory. Confirm that the project folder is "
              + "still accessible. If it is, reinstall the same checksummed release, then ask your IT or security "
              + "administrator to allow Nexus Harness and its bundled resources/runtime/python.exe. "
              + "Installing another Python will not fix this."
            : denied
              ? `The operating system refused permission to start ${command} (${code || "access denied"}). `
                + "Check file permissions, application-control policy, and antivirus before trying again."
              : `Could not start ${command}: ${error.message}`);
        problem.commandIsMissing = code === "ENOENT" && !projectUnavailable;
        problem.commandAccessDenied = denied;
        return problem;
      };
      let child;
      try {
        child = this.spawnProcess(command, argv, {
          cwd: projectPath,
      env: environmentForStarting(
        this.environment,
        whereTheHarnessLives(this.appFolder, projectPath, this.resources, {
          ...options, bundledRequired: this.bundledRequired,
        })
      ),
          windowsHide: true,
        });
      } catch (error) {
        reject(notHere(error));
        return;
      }
      this.children.add(child);
      this.child = child;
      child.once("close", () => {
        this.children.delete(child);
        if (this.child === child) {
          this.child = null;
          this.url = "";
        }
      });
      let settled = false;
      let becameReady = false;
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
          becameReady = true;
          this.url = ready.url;
          finish(null, ready.url);
        });
      };
      readStream(child.stdout);
      readStream(child.stderr);
      child.on("error", (error) => {
        // A spawn error has no live operating-system process and may not emit
        // close on every Node/Windows combination.
        this.children.delete(child);
        if (this.child === child) this.child = null;
        finish(notHere(error));
      });
      child.on("exit", (code) => {
        const detail = this.recentLog().split("\n").slice(-6).join("\n");
        finish(new Error(`${command} stopped with code ${code}.\n${detail}`));
        if (settled && this.child === child) {
          this.child = null;
          this.url = "";
          if (becameReady) this.onExit(code);
        }
      });
    });
  }

  stop(options = {}) {
    this.stopGeneration += 1;
    if (this.stopPromise) return this.stopPromise;
    const children = [...this.children];
    this.child = null;
    this.url = "";
    if (!children.length) return Promise.resolve(true);
    const requestedTimeout = Number(options.timeoutMs);
    const timeoutMs = Number.isFinite(requestedTimeout) && requestedTimeout >= 0
      ? requestedTimeout : STOP_TIMEOUT_MS;
    let tracked;
    const waiting = Promise.all(children.map((child) => new Promise((resolve) => {
      let timer = null;
      let finished = false;
      const finish = (closed) => {
        if (finished) return;
        finished = true;
        if (timer) clearTimeout(timer);
        child.removeListener?.("close", closedNormally);
        if (closed) this.children.delete(child);
        resolve(closed);
      };
      const closedNormally = () => finish(true);
      child.once?.("close", closedNormally);
      timer = setTimeout(() => finish(false), timeoutMs);
      try {
        // Terminating a Windows process is asynchronous. Wait for ChildProcess'
        // close event so its cwd and pipe handles are gone before Electron
        // itself exits; otherwise an immediate reinstall or project cleanup can
        // race a Python process that is still releasing the project directory.
        if (child.exitCode == null) child.kill();
        else finish(true);
      } catch (error) {
        // Keep this exact child in `children` unless Windows already reported
        // exit. A later stop call must retain authority to retry it.
        finish(child.exitCode != null);
      }
    }))).then((closed) => closed.every(Boolean));
    tracked = waiting.finally(() => {
      if (this.stopPromise === tracked) this.stopPromise = null;
    });
    this.stopPromise = tracked;
    return tracked;
  }
}

module.exports = { HarnessServer, whereTheHarnessLives, environmentForStarting, pythonCandidates, readReadyLine, isLoopbackUrl, isWebAddress, isOwnPage, READY_MARKER };
