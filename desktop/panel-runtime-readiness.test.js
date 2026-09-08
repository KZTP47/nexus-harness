"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const vm = require("node:vm");

const fixtures = [
  ["long-horizon", require("./long-horizon.smoke.js").waitForPanelRuntime, 120000],
  ["team-chat", require("./team-chat.smoke.js").waitForPanelRuntime, 180000],
];
const helpers = ["request", "activeConversationFor", "swarmChatIsHydrating"];

function pollingPage(states, timeout) {
  const observed = {probes: [], runtimeCalls: 0, configured: 0};
  return {
    observed,
    async waitForFunction(predicate, argument, options) {
      assert.equal(argument, null);
      assert.equal(options.timeout, timeout, "Startup must keep its existing finite bound");
      for (const state of states) {
        // Advance initialization without a wall-clock sleep or a real app profile.
        await Promise.resolve();
        assert.equal(observed.configured, 0, "Board setup ran before readiness resolved");
        const globals = {location: {protocol: state.protocol || "http:"},
          document: {readyState: state.readyState || "interactive"}};
        for (const name of state.helpers || []) globals[name] = () => {
          observed.runtimeCalls++;
          throw new Error(`Readiness must not invoke ${name}`);
        };
        Object.assign(globals, state.wrongTypes);
        const ready = vm.runInNewContext(`(${predicate.toString()})()`, globals);
        observed.probes.push(ready);
        if (ready) return;
      }
      throw new Error(`Panel runtime timed out after ${options.timeout}ms`);
    },
  };
}

for (const [name, waitForPanelRuntime, timeout] of fixtures) {
  test(`${name}: delayed renderer initialization completes before board setup`, async () => {
    const page = pollingPage([
      {protocol: "file:", helpers},
      {readyState: "loading", helpers},
      {},
      {helpers: ["request"]},
      {helpers},
    ], timeout);
    await waitForPanelRuntime(page);
    page.observed.configured++;
    assert.deepEqual(page.observed.probes, [false, false, false, false, true]);
    assert.equal(page.observed.configured, 1);
    assert.equal(page.observed.runtimeCalls, 0);
  });

  test(`${name}: an already initialized panel resolves on its first probe`, async () => {
    const page = pollingPage([{readyState: "complete", helpers}], timeout);
    await waitForPanelRuntime(page);
    assert.deepEqual(page.observed.probes, [true]);
    assert.equal(page.observed.runtimeCalls, 0);
  });

  for (const missing of helpers) {
    test(`${name}: missing or non-callable ${missing} times out without board setup`, async () => {
      const available = helpers.filter(one => one !== missing);
      const page = pollingPage([
        {helpers: available},
        {helpers: available, wrongTypes: {[missing]: {}}},
      ], timeout);
      await assert.rejects(async () => {
        await waitForPanelRuntime(page);
        page.observed.configured++;
      }, new RegExp(`Panel runtime timed out after ${timeout}ms`));
      assert.deepEqual(page.observed.probes, [false, false]);
      assert.equal(page.observed.configured, 0);
      assert.equal(page.observed.runtimeCalls, 0);
    });
  }
}
