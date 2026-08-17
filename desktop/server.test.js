"use strict";

const test = require("node:test");
const assert = require("node:assert");
const { EventEmitter } = require("node:events");

const { HarnessServer, readReadyLine, isLoopbackUrl, isOwnPage, pythonCandidates } = require("./server");

function fakeChild(behaviour) {
  const child = new EventEmitter();
  child.stdout = new EventEmitter();
  child.stderr = new EventEmitter();
  child.stdout.setEncoding = () => {};
  child.stderr.setEncoding = () => {};
  child.killed = false;
  child.kill = () => { child.killed = true; };
  // A test can say what this pretend Python does once someone is listening.
  if (behaviour) setImmediate(() => behaviour(child));
  return child;
}

test("the ready line is read out of mixed output", () => {
  const text = [
    "Harness UI: http://127.0.0.1:51234/",
    'harness-ui-ready {"url": "http://127.0.0.1:51234/", "port": 51234}',
    "Press Ctrl+C to stop.",
  ].join("\n");
  assert.deepStrictEqual(readReadyLine(text), { url: "http://127.0.0.1:51234/", port: 51234 });
});

test("output without a ready line gives nothing", () => {
  assert.strictEqual(readReadyLine("Harness UI: starting\nstill working\n"), null);
});

test("a broken ready line does not crash the reader", () => {
  assert.strictEqual(readReadyLine("harness-ui-ready {not json}"), null);
});

test("only loopback addresses are accepted", () => {
  assert.ok(isLoopbackUrl("http://127.0.0.1:8765/"));
  assert.ok(isLoopbackUrl("http://localhost:8765/"));
  assert.ok(isLoopbackUrl("http://[::1]:8765/"));
  assert.ok(!isLoopbackUrl("http://example.com/"));
  assert.ok(!isLoopbackUrl("https://127.0.0.1:8765/"));
  assert.ok(!isLoopbackUrl("file:///etc/passwd"));
  assert.ok(!isLoopbackUrl("not a url"));
});

test("a named Python command is tried first", () => {
  const found = pythonCandidates({ HARNESS_PYTHON: "custom-python" });
  assert.strictEqual(found[0][0], "custom-python");
  assert.strictEqual(found.length, 1);
});

test("starting resolves with the address the server printed", async () => {
  const child = fakeChild();
  const server = new HarnessServer({
    candidates: [["python", []]],
    spawn: () => child,
  });
  const started = server.start("demo-project");
  child.stdout.emit("data", 'harness-ui-ready {"url": "http://127.0.0.1:5000/", "port": 5000}\n');
  assert.strictEqual(await started, "http://127.0.0.1:5000/");
  assert.strictEqual(server.url, "http://127.0.0.1:5000/");
});

test("the project folder is passed to the harness", async () => {
  let seen = null;
  const child = fakeChild();
  const server = new HarnessServer({
    candidates: [["python", []]],
    spawn: (command, argv, options) => { seen = { command, argv, options }; return child; },
  });
  const started = server.start("demo project");
  child.stdout.emit("data", 'harness-ui-ready {"url": "http://127.0.0.1:1/", "port": 1}\n');
  await started;
  assert.strictEqual(seen.command, "python");
  assert.deepStrictEqual(seen.argv, [
    "-m", "our_harness", "--project", "demo project", "ui", "--port", "0", "--no-open-browser",
  ]);
  assert.strictEqual(seen.options.cwd, "demo project");
});

test("an address that is not on this machine is refused", async () => {
  const child = fakeChild();
  const server = new HarnessServer({ candidates: [["python", []]], spawn: () => child });
  const started = server.start("demo-project");
  child.stdout.emit("data", 'harness-ui-ready {"url": "http://evil.example/", "port": 80}\n');
  await assert.rejects(started, /not on this machine/);
  assert.ok(child.killed);
});

test("the next Python command is tried when the first is missing", async () => {
  const missing = fakeChild();
  const working = fakeChild();
  const server = new HarnessServer({
    candidates: [["py", ["-3"]], ["python", []]],
    spawn: (command) => (command === "py" ? missing : working),
  });
  const started = server.start("demo-project");
  // A command that is not on this machine always arrives with this code. It is
  // what tells the app to try the next one rather than stop and report.
  const notThere = new Error("not found");
  notThere.code = "ENOENT";
  missing.emit("error", notThere);
  await new Promise((resolve) => setImmediate(resolve));
  working.stdout.emit("data", 'harness-ui-ready {"url": "http://127.0.0.1:2/", "port": 2}\n');
  assert.strictEqual(await started, "http://127.0.0.1:2/");
});

test("a server that quits early reports what it printed", async () => {
  const child = fakeChild();
  const server = new HarnessServer({ candidates: [["python", []]], spawn: () => child });
  const started = server.start("demo-project");
  child.stderr.emit("data", "No module named our_harness\n");
  child.emit("exit", 1);
  await assert.rejects(started, /No module named our_harness/);
});

test("a slow start gives up with a clear message", async () => {
  const child = fakeChild();
  const server = new HarnessServer({ candidates: [["python", []]], spawn: () => child, timeoutMs: 20 });
  await assert.rejects(server.start("demo-project"), /within 0 seconds|did not open/);
  assert.ok(child.killed);
});

test("stopping kills the running server once", async () => {
  const child = fakeChild();
  const server = new HarnessServer({ candidates: [["python", []]], spawn: () => child });
  const started = server.start("demo-project");
  child.stdout.emit("data", 'harness-ui-ready {"url": "http://127.0.0.1:3/", "port": 3}\n');
  await started;
  server.stop();
  assert.ok(child.killed);
  assert.strictEqual(server.child, null);
  server.stop();
});

test("the kept log stays short", async () => {
  const server = new HarnessServer({ candidates: [["python", []]], spawn: () => fakeChild() });
  for (let index = 0; index < 500; index += 1) server.remember(`line ${index}`);
  assert.strictEqual(server.log.length, 200);
  assert.strictEqual(server.log.at(-1), "line 499");
});

test("the window may open this app's own pages", () => {
  const folder = "file:///somewhere/My%20Work/desktop/pages/";
  assert.ok(isOwnPage("file:///somewhere/My%20Work/desktop/pages/help.html", folder));
  assert.ok(isOwnPage("file:///somewhere/My Work/desktop/pages/help.html", folder),
    "a folder name with a space must still match");
  assert.ok(isOwnPage("file:///somewhere/My%20Work/desktop/pages/problem.html?title=x", folder));
});

test("the window may not open anything outside its pages folder", () => {
  const folder = "file:///somewhere/My%20Work/desktop/pages/";
  assert.ok(!isOwnPage("file:///other-place/secret.ini", folder));
  assert.ok(!isOwnPage("file:///somewhere/My%20Work/desktop/main.js", folder));
  assert.ok(!isOwnPage("file:///somewhere/My%20Work/desktop/pages/../main.js", folder));
  assert.ok(!isOwnPage("https://example.com/", folder));
  assert.ok(!isOwnPage("not a url", folder));
  assert.ok(!isOwnPage("file:///somewhere/My%20Work/desktop/pages/%ZZ", folder));
});

test("a Python that runs and then fails is the answer, not a reason to try the next one", async () => {
  // The real problem was here. Moving on would replace it with a complaint
  // about a command the person never chose.
  const tried = [];
  const server = new HarnessServer({
    candidates: [["py", ["-3"]], ["python", []], ["python3", []]],
    timeoutMs: 2000,
    spawn: (command) => {
      tried.push(command);
      return fakeChild((child) => {
        if (command === "py") {
          child.stderr.emit("data", "No module named our_harness\n");
          child.emit("exit", 1);
          return;
        }
        const error = new Error("spawn ENOENT");
        error.code = "ENOENT";
        child.emit("error", error);
      });
    },
  });
  await assert.rejects(
    () => server.start("demo-project"),
    (error) => {
      // The message names the Python that really ran, not the last one tried.
      assert.match(error.message, /^py stopped with code 1/);
      assert.match(error.message, /our_harness/);
      return true;
    }
  );
  assert.deepStrictEqual(tried, ["py", "python", "python3"],
    "every Python is still tried, because only one of them may have the harness");
});

test("a Python without the harness does not stop a later one that has it", async () => {
  // A machine often has several Pythons and only one with the harness in it.
  const tried = [];
  const server = new HarnessServer({
    candidates: [["py", ["-3"]], ["python", []]],
    timeoutMs: 2000,
    spawn: (command) => {
      tried.push(command);
      return fakeChild((child) => {
        if (command === "py") {
          child.stderr.emit("data", "No module named our_harness\n");
          child.emit("exit", 1);
          return;
        }
        child.stdout.emit("data", `harness-ui-ready {"url":"http://127.0.0.1:4321/"}\n`);
      });
    },
  });
  assert.strictEqual(await server.start("demo-project"), "http://127.0.0.1:4321/");
  assert.deepStrictEqual(tried, ["py", "python"]);
});

test("a command that is not on this machine moves on to the next one", async () => {
  const tried = [];
  const server = new HarnessServer({
    candidates: [["py", ["-3"]], ["python", []]],
    timeoutMs: 2000,
    spawn: (command) => {
      tried.push(command);
      return fakeChild((child) => {
        if (command === "py") {
          const error = new Error("spawn ENOENT");
          error.code = "ENOENT";
          child.emit("error", error);
          return;
        }
        child.stdout.emit("data", `harness-ui-ready {"url":"http://127.0.0.1:8765"}\n`);
      });
    },
  });
  assert.strictEqual(await server.start("demo-project"), "http://127.0.0.1:8765");
  assert.deepStrictEqual(tried, ["py", "python"]);
});

test("no Python at all is said plainly, naming what was tried", async () => {
  const server = new HarnessServer({
    candidates: [["py", ["-3"]], ["python", []], ["python3", []]],
    timeoutMs: 2000,
    spawn: () => fakeChild((child) => {
      const error = new Error("spawn ENOENT");
      error.code = "ENOENT";
      child.emit("error", error);
    }),
  });
  await assert.rejects(
    () => server.start("demo-project"),
    (error) => {
      assert.match(error.message, /No Python was found on this machine/);
      assert.match(error.message, /py, python, python3/);
      assert.match(error.message, /HARNESS_PYTHON/);
      return true;
    }
  );
});
