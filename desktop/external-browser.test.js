"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {EventEmitter} = require("node:events");
const path = require("node:path");
const {
  ExternalBrowserTransport, ExternalPageContents, findInstalledBrowser,
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
    backgroundMode: true,
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
  assert.ok(launched[0].args.includes("--start-minimized"));
  assert.ok(!launched[0].args.some((one) => /enable-automation|headless|remote-debugging-port=0/.test(one)));
  assert.equal(launched[0].options.windowsHide, false);
  await transport.close();
  assert.equal(contents.isDestroyed(), true);
});

test("background mode minimizes ordinary provider windows through the loopback CDP session", async () => {
  const commands = [];
  let detached = 0;
  const page = {isClosed: () => false};
  const context = {
    pages: () => [page],
    newCDPSession: async (selected) => {
      assert.equal(selected, page);
      return {
        send: async (method, payload) => {
          commands.push([method, payload]);
          return method === "Browser.getWindowForTarget" ? {windowId: 42} : {};
        },
        detach: async () => { detached += 1; },
      };
    },
  };
  const transport = new ExternalBrowserTransport({
    provider: {id: "claude", label: "Claude", home: "https://claude.ai/"},
    profilePath: "nexus-test-profile",
  });
  transport.context = context;

  assert.equal(await transport.setBackgroundMode(true), true);

  assert.deepEqual(commands, [
    ["Browser.getWindowForTarget", undefined],
    ["Browser.setWindowBounds", {windowId: 42, bounds: {windowState: "minimized"}}],
  ]);
  assert.equal(detached, 1);
});

test("the first restored provider tab navigates to the requested saved conversation", async () => {
  const navigations = [];
  const page = {
    url: () => "https://claude.ai/new",
    isClosed: () => false,
    goto: async (url, options) => navigations.push([url, options]),
    bringToFront: async () => {},
  };
  const transport = Object.create(ExternalBrowserTransport.prototype);
  transport.provider = {
    home: "https://claude.ai/", newChat: "https://claude.ai/new",
  };
  transport.initialClaimed = false;
  transport.initialPage = page;
  transport.start = async () => ({newPage: async () => { throw new Error("unexpected page"); }});

  const requested = "https://claude.ai/chat/saved-conversation";
  const opened = await transport.openPage(requested);

  assert.equal(opened, page);
  assert.equal(transport.initialClaimed, true);
  assert.deepEqual(navigations, [[requested, {
    waitUntil: "domcontentloaded", timeout: 90000,
  }]]);
});

test("a Claude sign-in replacement tab is adopted as the current provider chat", async () => {
  const oldPage = Object.assign(new EventEmitter(), {
    url: () => "https://claude.ai/login",
    title: async () => "Sign in - Claude",
    mainFrame() { return this; }, isClosed: () => true,
  });
  const conversation = Object.assign(new EventEmitter(), {
    url: () => "https://claude.ai/chat/current-conversation",
    title: async () => "Current Claude conversation",
    mainFrame() { return this; }, isClosed: () => false,
    evaluate: async () => ({focused: false, visibility: "visible"}),
  });
  const context = {pages: () => [oldPage, conversation]};
  const transport = {
    provider: {id: "claude", label: "Claude", home: "https://claude.ai/", hosts: ["claude.ai"]},
    openPage: async () => oldPage,
    currentProviderPage: async () => conversation,
  };
  const contents = new ExternalPageContents(transport, "https://claude.ai/login");
  // Construction starts openPage on the next microtask; make the initial tab
  // live long enough to bind, then emulate OAuth closing it.
  oldPage.isClosed = () => false;
  await contents.ready;
  oldPage.isClosed = () => true;
  oldPage.emit("close");

  const adopted = await contents.useCurrentPage();

  assert.deepEqual(adopted, {
    url: "https://claude.ai/chat/current-conversation",
    title: "Current Claude conversation",
  });
  assert.equal(contents.isDestroyed(), false);
  assert.equal(contents.page, conversation);
  assert.equal(context.pages().length, 2);
});

test("current Claude tab selection prefers the visible provider conversation", async () => {
  const page = (url, visibility, focused = false) => ({
    url: () => url, isClosed: () => false,
    evaluate: async () => ({visibility, focused}),
  });
  const home = page("https://claude.ai/", "hidden");
  const attacker = page("https://claude.ai.attacker.example/chat/wrong", "visible", true);
  const conversation = page("https://claude.ai/chat/right", "visible");
  const transport = new ExternalBrowserTransport({
    provider: {
      id: "claude", label: "Claude", home: "https://claude.ai/", hosts: ["claude.ai"],
    },
    profilePath: "nexus-test-profile",
  });
  transport.start = async () => ({pages: () => [home, attacker, conversation]});

  assert.equal(await transport.currentProviderPage(), conversation);
});

test("external provider submission uses real keyboard input and pointer activation", async () => {
  const keys = [];
  const clicks = [];
  const evaluations = [];
  const page = Object.assign(new EventEmitter(), {
    url: () => "https://claude.ai/new", title: async () => "Claude",
    mainFrame() { return this; }, isClosed: () => false,
    keyboard: {
      press: async (key) => keys.push(["press", key]),
      insertText: async (text) => keys.push(["text", text]),
    },
    evaluate: async (script) => {
      evaluations.push(String(script));
      return evaluations.length === 1
        ? true : {x: 500, y: 400, fingerprint: "BUTTON|send"};
    },
    mouse: {click: async (x, y) => clicks.push([x, y])},
  });
  const transport = {
    provider: {label: "Claude"}, openPage: async () => page,
    currentProviderPage: async () => page,
  };
  const contents = new ExternalPageContents(transport, page.url());
  await contents.ready;

  const activated = await contents.replaceTextAndSubmit("NEXUS_NATIVE_INPUT", {
    composer: ["[contenteditable='true']"], send: ["button[aria-label*='Send']"],
  });

  assert.deepEqual(keys, [["text", "NEXUS_NATIVE_INPUT"]]);
  assert.ok(evaluations[0].includes("selectNodeContents"));
  assert.deepEqual(clicks, [[500, 400]]);
  assert.equal(activated.sendActivated, true);
});
