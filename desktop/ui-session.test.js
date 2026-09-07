"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../src/our_harness/ui/app.js"), "utf8");
const implementation = source.slice(source.indexOf("function bootstrapSession()"), source.indexOf("// Asking somebody for one line"));

function fixture() {
  let release;
  const waiting = new Promise((resolve) => { release = resolve; });
  const calls = [];
  const context = vm.createContext({fetch: async (url, options) => {
    calls.push({url, options});
    if (url === "/api/bootstrap") return waiting;
    return {ok: true, json: async () => ({board: {agents: []}})};
  }});
  vm.runInContext("let token = ''; let sessionBootstrap = null;\n" + implementation, context);
  return {context, calls, release, run: (code) => vm.runInContext(code, context)};
}

test("an early Swarm read and boot share bootstrap and never dispatch an empty token", async () => {
  const f = fixture();
  const board = f.run('request("/api/swarm?refresh_providers=false")');
  const boot = f.run("bootstrapSession()");
  const inventory = f.run('request("/api/long-horizon/goals")');
  assert.deepEqual(f.calls.map((one) => one.url), ["/api/bootstrap"]);
  f.release({ok: true, json: async () => ({token: "fresh-local-session"})});
  await Promise.all([board, boot, inventory]);
  assert.equal(f.calls.filter((one) => one.url === "/api/bootstrap").length, 1);
  assert.equal(f.calls.length, 3);
  for (const call of f.calls.slice(1)) assert.equal(call.options.headers["X-Harness-Token"], "fresh-local-session");
});

test("failed bootstrap prevents both board hydration and provider submission without automatic retries", async () => {
  const f = fixture();
  const board = f.run('request("/api/swarm?refresh_providers=false")');
  const send = f.run('request("/api/swarm/say", {method: "POST", body: "authorized draft"})');
  f.release({ok: false, status: 503, json: async () => ({error: "Local service unavailable"})});
  await assert.rejects(board, /Local service unavailable/);
  await assert.rejects(send, /Local service unavailable/);
  await assert.rejects(f.run('request("/api/swarm")'), /Local service unavailable/);
  assert.deepEqual(f.calls.map((one) => one.url), ["/api/bootstrap"]);
});

test("a tokenless bootstrap response fails before any protected dispatch", async () => {
  const f = fixture();
  const board = f.run('request("/api/swarm")');
  f.release({ok: true, json: async () => ({})});
  await assert.rejects(board, /could not establish/);
  assert.equal(f.calls.length, 1);
});

test("a provider request rejected after session establishment is sent exactly once", async () => {
  const f = fixture();
  f.context.fetch = async (url, options) => {
    f.calls.push({url, options});
    return url === "/api/bootstrap"
      ? {ok: true, json: async () => ({token: "session"})}
      : {ok: false, status: 502, json: async () => ({error: "Delivery unknown"})};
  };
  await assert.rejects(f.run('request("/api/swarm/say", {method: "POST", body: "one message"})'), /Delivery unknown/);
  assert.deepEqual(f.calls.map((one) => one.url), ["/api/bootstrap", "/api/swarm/say"]);
});
