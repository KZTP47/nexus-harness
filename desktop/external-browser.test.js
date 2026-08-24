"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {EventEmitter} = require("node:events");
const path = require("node:path");
const {
  ExternalBrowserTransport, findInstalledBrowser,
} = require("./external-browser");

test("installed browser selection follows provider preference and exact executable checks", () => {
  const root = "C:" + path.win32.sep;
  const candidates = [
    {family: "chrome", executable: path.win32.join(root, "Chrome", "chrome.exe")},
    {family: "edge", executable: path.win32.join(root, "Edge", "msedge.exe")},
  ];
  assert.deepEqual(findInstalledBrowser(["edge", "chrome"], {
    candidates, exists: (candidate) => candidate.includes("Edge"),
  }), candidates[1]);
  assert.equal(findInstalledBrowser(["chrome"], {
    candidates, exists: () => false,
  }), null);
});

test("the external transport starts a normal loopback-controlled browser without automation flags", async () => {
  const root = "C:" + path.win32.sep;
  const profilePath = path.win32.join(root, "Nexus", "external-web-chat", "claude");
  const chromePath = path.win32.join(root, "Chrome", "chrome.exe");
  const launched = [];
  const page = Object.assign(new EventEmitter(), {
    url: () => "https://claude.ai/login",
    title: async () => "Sign in - Claude",
    mainFrame() { return this; },
    isClosed: () => false,
    bringToFront: async () => {},
    close: async () => page.emit("close"),
  });
  const context = {
    pages: () => [page],
    newPage: async () => { throw new Error("the initial page should be claimed"); },
  };
  const browser = Object.assign(new EventEmitter(), {
    contexts: () => [context], isConnected: () => true, close: async () => {},
  });
  const child = Object.assign(new EventEmitter(), {
    exitCode: null, killed: false, kill() { this.killed = true; },
  });
  const transport = new ExternalBrowserTransport({
    provider: {id: "claude", label: "Claude", home: "https://claude.ai/"},
    profilePath,
    preferred: ["chrome", "edge"],
    findBrowser: () => ({family: "chrome", executable: chromePath}),
    reservePort: async () => 23456,
    ensureDirectory: (directory) => assert.equal(
      directory, profilePath),
    spawn: (executable, args, options) => {
      launched.push({executable, args, options});
      return child;
    },
    chromium: {connectOverCDP: async (endpoint) => {
      assert.equal(endpoint, "http://127.0.0.1:23456");
      return browser;
    }},
  });

  const contents = transport.createContents("https://claude.ai/");
  await contents.ready;

  assert.equal(contents.getURL(), "https://claude.ai/login");
  assert.equal(contents.getTitle(), "Sign in - Claude");
  assert.equal(launched.length, 1);
  assert.equal(launched[0].executable, chromePath);
  assert.ok(launched[0].args.includes("--remote-debugging-port=23456"));
  assert.ok(launched[0].args.includes("--remote-debugging-address=127.0.0.1"));
  assert.ok(launched[0].args.includes(`--user-data-dir=${profilePath}`));
  assert.ok(!launched[0].args.some((one) => /enable-automation|headless|remote-debugging-port=0/.test(one)));
  assert.equal(launched[0].options.windowsHide, false);
  await transport.close();
  assert.equal(contents.isDestroyed(), true);
});
