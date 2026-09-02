"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const {createShutdownCoordinator} = require("./shutdown");

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return {promise, resolve, reject};
}

function nextTurn() {
  return new Promise((resolve) => setImmediate(resolve));
}

test("duplicate requests coalesce onto the exact same shutdown promise", async () => {
  let webChatCloses = 0;
  let serverStops = 0;
  let quits = 0;
  const coordinator = createShutdownCoordinator({
    closeWebChats: async () => { webChatCloses += 1; },
    stopServer: async () => { serverStops += 1; return true; },
    quit: () => { quits += 1; },
    closeWindow: () => assert.fail("quit request must not close only the window"),
  });

  const first = coordinator.request("quit");
  const second = coordinator.request("quit");

  assert.strictEqual(second, first);
  await first;
  assert.equal(webChatCloses, 1);
  assert.equal(serverStops, 1);
  assert.equal(quits, 1);
  assert.equal(coordinator.isReady(), true);
});

test("quit escalates an in-flight close request and uses the shared promise", async () => {
  const chatsClosed = deferred();
  const serverStopped = deferred();
  let quits = 0;
  let windowCloses = 0;
  const coordinator = createShutdownCoordinator({
    closeWebChats: () => chatsClosed.promise,
    stopServer: () => serverStopped.promise,
    quit: () => { quits += 1; },
    closeWindow: () => { windowCloses += 1; },
  });

  const closing = coordinator.request("close");
  const quitting = coordinator.request("quit");
  assert.strictEqual(quitting, closing);

  chatsClosed.resolve();
  serverStopped.resolve(true);
  await closing;

  assert.equal(quits, 1);
  assert.equal(windowCloses, 0);
});

test("server rejection and false result retry while web-chat rejection is swallowed", async () => {
  const stopResults = [new Error("transient stop failure"), false, true];
  const delays = [];
  let serverStops = 0;
  let quits = 0;
  const coordinator = createShutdownCoordinator({
    closeWebChats: async () => { throw new Error("optional chat cleanup failed"); },
    stopServer: async () => {
      const result = stopResults[serverStops++];
      if (result instanceof Error) throw result;
      return result;
    },
    quit: () => { quits += 1; },
    closeWindow: () => assert.fail("quit request must not close only the window"),
    delay: async (milliseconds) => { delays.push(milliseconds); },
    retryDelayMs: 37,
  });

  await coordinator.request("quit");

  assert.equal(serverStops, 3);
  assert.deepEqual(delays, [37, 37]);
  assert.equal(quits, 1);
  assert.equal(coordinator.isReady(), true);
});

test("close-only waits for chat and server cleanup running in parallel", async () => {
  const chatsClosed = deferred();
  let serverStopCalled = false;
  let windowCloses = 0;
  let quits = 0;
  const coordinator = createShutdownCoordinator({
    closeWebChats: () => chatsClosed.promise,
    stopServer: async () => { serverStopCalled = true; return true; },
    quit: () => { quits += 1; },
    closeWindow: () => { windowCloses += 1; },
  });

  const closing = coordinator.request("close");
  await nextTurn();
  assert.equal(serverStopCalled, true, "server shutdown starts without waiting for web chats");
  assert.equal(coordinator.isReady(), false);
  assert.equal(windowCloses, 0);

  chatsClosed.resolve();
  await closing;

  assert.equal(coordinator.isReady(), true);
  assert.equal(windowCloses, 1);
  assert.equal(quits, 0);
});
