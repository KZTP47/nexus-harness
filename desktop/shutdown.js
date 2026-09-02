"use strict";

const DEFAULT_RETRY_DELAY_MS = 100;

function defaultDelay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function createShutdownCoordinator({
  closeWebChats,
  stopServer,
  quit,
  closeWindow,
  delay = defaultDelay,
  retryDelayMs = DEFAULT_RETRY_DELAY_MS,
}) {
  for (const [name, dependency] of Object.entries({
    closeWebChats, stopServer, quit, closeWindow, delay,
  })) {
    if (typeof dependency !== "function") {
      throw new TypeError(`${name} must be a function`);
    }
  }
  if (!Number.isFinite(retryDelayMs) || retryDelayMs < 0) {
    throw new TypeError("retryDelayMs must be a non-negative finite number");
  }

  let requestedMode = null;
  let sharedPromise = null;
  let ready = false;

  async function closeWebChatsOnce() {
    try {
      await closeWebChats();
    } catch (_error) {
      // Closing the application must not be held hostage by a failed optional
      // browser-chat cleanup. Native resources still get their normal teardown.
    }
  }

  async function stopServerUntilReady() {
    for (;;) {
      let stopped = false;
      try {
        stopped = (await stopServer()) === true;
      } catch (_error) {
        stopped = false;
      }
      if (stopped) return;
      await delay(retryDelayMs);
    }
  }

  async function coordinate() {
    await Promise.all([
      closeWebChatsOnce(),
      stopServerUntilReady(),
    ]);

    // Event handlers can now allow the close/before-quit event emitted by the
    // selected finisher instead of recursively starting another shutdown.
    ready = true;
    const finish = requestedMode === "quit" ? quit : closeWindow;
    await finish();
  }

  function request(mode) {
    if (mode !== "close" && mode !== "quit") {
      throw new TypeError('shutdown mode must be "close" or "quit"');
    }
    if (mode === "quit") requestedMode = "quit";
    else if (requestedMode === null) requestedMode = "close";

    if (!sharedPromise) sharedPromise = coordinate();
    return sharedPromise;
  }

  return {
    request,
    isReady() {
      return ready;
    },
  };
}

module.exports = {createShutdownCoordinator};
