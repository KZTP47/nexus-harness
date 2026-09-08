"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");
const source = fs.readFileSync(path.join(__dirname, "../src/our_harness/ui/app.js"), "utf8");
const code = source.slice(source.indexOf("function renderAgentRepairPanel"),
  source.indexOf("async function checkAgentLogin"));

function fixture() {
  const nodes = new Map(), calls = [], timers = new Map();
  let serial = 0;
  const context = vm.createContext({AbortController, Promise,
    swarmPicked: {kind: "agent", id: "a"},
    swarmAgentRepairChecks: new Map(), swarmAgentRepairPlans: new Map(),
    swarmAgentRepairTests: new Map(),
    theSwarmAgent(id) { return {id, who: "route-a", ready: true}; },
    agentStillUsesRoute(id, route) { return route === context.route; },
    agentRepairContext(id) { return {agent_id: id}; },
    route: "route-a",
    $(id) {
      if (!nodes.has(id)) nodes.set(id, {dataset: {}, textContent: "", children: [],
        isConnected: true, classList: {toggle() {}}, removeAttribute() {},
        replaceChildren() { this.children = []; }, append(value) { this.children.push(value); }});
      return nodes.get(id);
    },
    make() { return {dataset: {}}; },
    setTimeout(fn, ms) { assert.equal(ms, 30000); timers.set(++serial, fn); return serial; },
    clearTimeout(id) { timers.delete(id); },
    request(url, options) {
      return new Promise((resolve, reject) => calls.push({url, options, resolve, reject}));
    },
  });
  vm.runInContext(code, context);
  const redraw = () => context.renderAgentRepairPanel(context.theSwarmAgent("a"), context.route,
    context.swarmAgentRepairPlans.get("a")?.plan);
  redraw();
  const button = context.$("swarmAgentRepairStart");
  return {context, calls, timers, button, redraw,
    start: () => context.loadAgentRepairPlan("a", context.route, button),
    badge: () => context.$("swarmAgentRepairBadge").textContent,
    status: () => context.$("swarmAgentSessionStatus").textContent};
}
const plan = {repair: {state: "ready", title: "Checked route", summary: "Ready", steps: [], actions: []}};

test("diagnosis survives redraws, coalesces repeat clicks and retains success", async () => {
  const f = fixture(); const pending = f.start();
  f.redraw();
  assert.equal(f.badge(), "Checking");
  assert.equal(f.button.disabled, true);
  assert.match(f.status(), /30 seconds/);
  await f.start(); assert.equal(f.calls.length, 1);
  assert.deepEqual(JSON.parse(f.calls[0].options.body), {route: "route-a", agent_id: "a"});
  f.calls[0].resolve(plan); await pending; f.redraw();
  assert.equal(f.badge(), "ready");
  assert.equal(f.timers.size, 0);
});

test("a never-returning diagnosis times out, remains retryable after redraw, and ignores late success", async () => {
  const f = fixture(); const pending = f.start();
  [...f.timers.values()][0](); await pending; f.redraw();
  assert.equal(f.calls[0].options.signal.aborted, true);
  assert.equal(f.button.disabled, false);
  assert.equal(f.button.textContent, "Check again");
  assert.equal(f.badge(), "Check failed");
  assert.match(f.status(), /did not finish within 30 seconds/);
  const retry = f.start(); f.calls[0].resolve(plan); await Promise.resolve();
  assert.equal(f.context.swarmAgentRepairPlans.size, 0);
  f.calls[1].resolve(plan); await retry;
  assert.equal(f.badge(), "ready");
});

test("HTTP failure stays visible across refresh and a second check can succeed", async () => {
  const f = fixture(); const pending = f.start();
  f.calls[0].reject(new Error("Service unavailable")); await pending; f.redraw();
  assert.equal(f.status(), "Service unavailable"); assert.equal(f.button.disabled, false);
  const retry = f.start(); f.calls[1].resolve(plan); await retry;
  assert.equal(f.status(), "Ready");
});

test("late responses cannot overwrite another selected agent's panel", async () => {
  const f = fixture(); const pending = f.start();
  f.context.swarmPicked = {kind: "agent", id: "b"};
  f.context.renderAgentRepairPanel(f.context.theSwarmAgent("b"), "route-b");
  const before = f.status(); f.calls[0].resolve(plan); await pending;
  assert.equal(f.status(), before);
  assert.equal(f.context.swarmAgentRepairPlans.get("a").plan, plan);
});

test("a changed route supersedes pending diagnosis and cannot receive its old result", async () => {
  const f = fixture(); const first = f.start();
  f.context.route = "route-b"; const second = f.start();
  assert.equal(f.calls[0].options.signal.aborted, true);
  f.calls[0].resolve(plan); await first;
  assert.equal(f.context.swarmAgentRepairPlans.size, 0);
  assert.equal(f.badge(), "Checking");
  f.calls[1].resolve(plan); await second;
  assert.equal(f.context.swarmAgentRepairPlans.get("a").route, "route-b");
});
