"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const path = require("node:path");
const { MailNotifier, sanitizeNotice, TOAST_WIDTH, MARGIN, RETRY_BROKEN_MS } = require("./mail-notifier");

function fakeElectron(workArea = { x: 0, y: 0, width: 1920, height: 1040 }) {
  const made = [];
  class FakeWindow extends EventEmitter {
    constructor(options) {
      super();
      this.options = options;
      this.visible = false;
      this.destroyed = false;
      this.inactiveShows = 0;
      this.focusedShows = 0;
      this.webContents = new EventEmitter();
      this.webContents.sent = [];
      this.webContents.send = (channel, value) => this.webContents.sent.push([channel, value]);
      made.push(this);
    }
    loadURL(url) { this.url = url; }
    setAlwaysOnTop(on, level) { this.onTop = [on, level]; }
    setBounds(bounds) { this.bounds = bounds; }
    isVisible() { return this.visible; }
    showInactive() { this.visible = true; this.inactiveShows += 1; }
    show() { this.visible = true; this.focusedShows += 1; }
    hide() { this.visible = false; }
    isDestroyed() { return this.destroyed; }
    destroy() { this.destroyed = true; this.emit("closed"); }
  }
  return { made, electron: { BrowserWindow: FakeWindow, screen: { getPrimaryDisplay: () => ({ workArea }) } } };
}

const notice = (id, extra = {}) => ({
  id, title: "Ada Lovelace", action: "New email · Nexus AI is drafting a reply", detail: "Quarterly numbers",
  avatar: "A", account_id: "acct1", message_id: "msg1", draft_id: "draft1", ...extra,
});

test("notices are plain bounded text and identifiers, never markup targets", () => {
  assert.equal(sanitizeNotice(null), null);
  assert.equal(sanitizeNotice({ id: "../../etc", title: "x" }), null, "unsafe id is refused");
  assert.equal(sanitizeNotice({ id: "ok-1", title: "   " }), null, "a card needs a title");
  const cleaned = sanitizeNotice(notice("ok-1", {
    title: "Ada\u2028<img src=x onerror=alert(1)>\n" + "x".repeat(500),
    message_id: "javascript:alert(1)", avatar: "Ada",
  }));
  assert.equal(cleaned.title.length, 200);
  assert.ok(!/[\n\u2028]/.test(cleaned.title));
  assert.equal(cleaned.message_id, "", "a target id that is not an identifier is dropped");
  assert.equal(cleaned.avatar, "Ad");
});

test("the pop-up waits for its page, sits in the corner and never takes focus", async () => {
  const { made, electron } = fakeElectron({ x: 0, y: 0, width: 1920, height: 1040 });
  let guarded = 0;
  const notifier = new MailNotifier({ electron, page: "file:///mail-toast.html", preload: "p.js", guard: () => { guarded += 1; } });
  const first = notifier.show(notice("n-1"));
  assert.ok(first instanceof Promise, "a card waiting for the page is not yet reported as shown");
  assert.equal(made.length, 1);
  const popup = made[0];
  assert.equal(popup.options.focusable, false);
  assert.equal(popup.options.skipTaskbar, true);
  assert.equal(popup.options.webPreferences.contextIsolation, true);
  assert.equal(popup.options.webPreferences.sandbox, true);
  assert.equal(popup.options.webPreferences.nodeIntegration, false);
  assert.equal(guarded, 1);
  assert.deepEqual(popup.webContents.sent, [], "nothing is sent before the page loads");
  popup.webContents.emit("did-finish-load");
  assert.equal(popup.webContents.sent.length, 1);
  assert.equal(popup.webContents.sent[0][0], "mail-toast:show");
  assert.equal(await first, true, "it is reported once the loaded page has it");
  assert.equal(notifier.show(notice("n-2")), true);
  assert.equal(made.length, 1, "one window holds the whole stack");
  assert.equal(popup.webContents.sent.length, 2);

  assert.equal(notifier.resize({}, 100), false, "only the pop-up page may resize the pop-up");
  assert.equal(notifier.resize(popup.webContents, 188), true);
  assert.deepEqual(popup.bounds, {
    x: 1920 - TOAST_WIDTH - MARGIN, y: 1040 - 188 - MARGIN, width: TOAST_WIDTH, height: 188,
  });
  assert.equal(popup.inactiveShows, 1);
  assert.equal(popup.focusedShows, 0, "showing a notice must not steal focus");
  notifier.resize(popup.webContents, 0);
  assert.equal(popup.visible, false, "an empty stack hides the pop-up");
});

test("a click opens exactly the announced email, and only from the pop-up itself", () => {
  const { made, electron } = fakeElectron();
  const opened = [];
  const notifier = new MailNotifier({ electron, page: "file:///mail-toast.html", onActivate: (target) => opened.push(target) });
  notifier.show(notice("n-1"));
  const popup = made[0];
  assert.equal(notifier.activate({}, "n-1"), false);
  assert.equal(notifier.activate(popup.webContents, "unknown"), false);
  assert.equal(notifier.activate(popup.webContents, "n-1"), true);
  assert.deepEqual(opened, [{ account_id: "acct1", message_id: "msg1", draft_id: "draft1" }]);
});

test("closing the app removes the pop-up, and a later notice makes a fresh one", () => {
  const { made, electron } = fakeElectron();
  const notifier = new MailNotifier({ electron, page: "file:///mail-toast.html" });
  notifier.show(notice("n-1"));
  notifier.close();
  assert.equal(made[0].destroyed, true);
  notifier.show(notice("n-2"));
  assert.equal(made.length, 2);
  assert.notEqual(made[1], made[0]);
});

test("the pop-up page writes text only and the app ships every part of it", () => {
  const script = fs.readFileSync(path.join(__dirname, "pages", "mail-toast.js"), "utf8");
  assert.ok(!/innerHTML|insertAdjacentHTML|outerHTML|document\.write/.test(script));
  const main = fs.readFileSync(path.join(__dirname, "main.js"), "utf8");
  assert.match(main, /pageUrl\("mail-toast\.html"\)/);
  assert.ok(fs.existsSync(path.join(__dirname, "pages", "mail-toast.html")));
  const files = require("./package.json").build.files;
  for (const needed of ["mail-notifier.js", "mail-toast-preload.js"]) assert.ok(files.includes(needed), needed);
});

test("a crashed pop-up is replaced by a fresh one instead of swallowing later notices", async () => {
  const { made, electron } = fakeElectron();
  const notifier = new MailNotifier({ electron, page: "file:///mail-toast.html", preload: "p.js" });
  notifier.show(notice("n-1"));
  made[0].webContents.emit("did-finish-load");
  made[0].visible = true;
  made[0].webContents.emit("render-process-gone", {}, { reason: "crashed" });
  assert.equal(made[0].destroyed, true, "the dead always-on-top window does not stay in the corner");
  const second = notifier.show(notice("n-2"));
  assert.equal(made.length, 2, "the next notice builds a new pop-up");
  made[1].webContents.emit("did-finish-load");
  assert.equal(await second, true);
  assert.deepEqual(made[1].webContents.sent.map(([, value]) => value.id), ["n-2"]);
  // A hung page is treated the same way.
  made[1].webContents.emit("unresponsive");
  assert.equal(made[1].destroyed, true);
});

test("a pop-up page that never loads keeps only the newest notices and then lets the page show its own", async () => {
  const { made, electron } = fakeElectron();
  const notifier = new MailNotifier({ electron, page: "file:///mail-toast.html", preload: "p.js" });
  const answers = [];
  for (let n = 1; n <= 1000; n += 1) answers.push(notifier.show(notice("n-" + n)));
  assert.deepEqual(notifier.waiting.map(item => item.id), ["n-998", "n-999", "n-1000"], "the wait queue is bounded");
  assert.equal(await answers[0], false, "an older card that cannot wait goes back to the page's own card");
  // A subframe failure is not the pop-up page failing.
  made[0].webContents.emit("did-fail-load", {}, -2, "failed", "about:blank", false);
  assert.equal(made[0].destroyed, false);
  made[0].webContents.emit("did-fail-load", {}, -6, "file not found", "file:///mail-toast.html", true);
  assert.equal(made[0].destroyed, true);
  assert.equal(made.length, 2, "the waiting cards are carried to one fresh pop-up");
  assert.deepEqual(notifier.waiting.map(item => item.id), ["n-998", "n-999", "n-1000"]);
  const late = notifier.show(notice("n-1001"));
  made[1].webContents.emit("did-fail-load", {}, -6, "file not found", "file:///mail-toast.html", true);
  // The caller's cursor has moved past these cards: each comes back to be shown in the page.
  assert.deepEqual(await Promise.all([answers[997], answers[998], answers[999], late]), [false, false, false, false]);
  assert.deepEqual(notifier.waiting, []);
  assert.equal(notifier.show(notice("n-1002")), false, "after repeated failures the caller falls back to its in-page card");
  assert.equal(made.length, 2, "no endless stream of broken windows");
});

test("a pop-up that broke twice gets one fresh attempt after a while", async () => {
  const { made, electron } = fakeElectron();
  let clock = 1000;
  const notifier = new MailNotifier({ electron, page: "file:///mail-toast.html", preload: "p.js", now: () => clock });
  const first = notifier.show(notice("n-1"));
  made[0].webContents.emit("render-process-gone", {}, { reason: "oom" });
  made[1].webContents.emit("render-process-gone", {}, { reason: "oom" });
  assert.equal(await first, false);
  clock += RETRY_BROKEN_MS - 1;
  assert.equal(notifier.show(notice("n-2")), false, "not while the cause is probably still there");
  assert.equal(made.length, 2);
  clock += 1;
  const retried = notifier.show(notice("n-3"));
  assert.equal(made.length, 3, "one fresh pop-up");
  made[2].webContents.emit("did-fail-load", {}, -6, "file not found", "file:///mail-toast.html", true);
  assert.equal(await retried, false);
  assert.equal(notifier.show(notice("n-4")), false, "a failed retry waits again instead of looping");
  clock += RETRY_BROKEN_MS;
  const healthy = notifier.show(notice("n-5"));
  made[3].webContents.emit("did-finish-load");
  assert.equal(await healthy, true);
  assert.equal(notifier.show(notice("n-6")), true, "a page that loads clears the failure count");
});

test("a pop-up page that neither loads nor fails is given up on", async () => {
  const { made, electron } = fakeElectron();
  const notifier = new MailNotifier({ electron, page: "file:///mail-toast.html", preload: "p.js", loadTimeoutMs: 5 });
  const answer = notifier.show(notice("n-1"));
  await new Promise(resolve => setTimeout(resolve, 30));
  assert.equal(made[0].destroyed, true);
  notifier.close();
  assert.equal(await answer, false);
});

test("a pop-up that loaded once and later crashed is still retried", async () => {
  const { made, electron } = fakeElectron();
  const notifier = new MailNotifier({ electron, page: "file:///mail-toast.html", preload: "p.js" });
  for (let round = 0; round < 4; round += 1) {
    const answer = notifier.show(notice("n-" + round));
    made[round].webContents.emit("did-finish-load");
    assert.equal(await answer, true);
    made[round].webContents.emit("render-process-gone", {}, { reason: "oom" });
  }
  assert.equal(made.length, 4);
});

test("destroying the pop-up while its page loads does not leave an unhandled rejection", async () => {
  const { made, electron } = fakeElectron();
  electron.BrowserWindow.prototype.loadURL = function loadURL() { return Promise.reject(new Error("ERR_ABORTED (-3)")); };
  const unhandled = [];
  const listener = reason => unhandled.push(reason);
  process.on("unhandledRejection", listener);
  try {
    const notifier = new MailNotifier({ electron, page: "file:///mail-toast.html", preload: "p.js" });
    notifier.show(notice("n-1"));
    notifier.close();
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(unhandled, []);
    assert.equal(made[0].destroyed, true);
  } finally { process.off("unhandledRejection", listener); }
});

test("a card offered again by a reloaded page or second window is shown once", async () => {
  const { made, electron } = fakeElectron();
  const notifier = new MailNotifier({ electron, page: "file:///mail-toast.html", preload: "p.js" });
  const first = notifier.show(notice("n-1"));
  assert.equal(notifier.show(notice("n-1")), first, "a second offer while it waits shares the first answer");
  made[0].webContents.emit("did-finish-load");
  assert.equal(await first, true);
  assert.equal(notifier.show(notice("n-1")), true, "already on screen counts as shown");
  assert.deepEqual(made[0].webContents.sent.map(([, value]) => value.id), ["n-1"]);
  notifier.show(notice("n-2"));
  assert.deepEqual(made[0].webContents.sent.map(([, value]) => value.id), ["n-1", "n-2"]);
});

test("cards waiting for a pop-up that broke reach the next one, and a refused card can be offered again", async () => {
  const { made, electron } = fakeElectron();
  const notifier = new MailNotifier({ electron, page: "file:///mail-toast.html", preload: "p.js" });
  const carried = notifier.show(notice("n-1"));
  made[0].webContents.emit("did-fail-load", {}, -6, "file not found", "file:///mail-toast.html", true);
  made[1].webContents.emit("did-finish-load");
  assert.deepEqual(made[1].webContents.sent.map(([, value]) => value.id), ["n-1"]);
  assert.equal(await carried, true);
  // A card handed back to the page is not remembered as shown.
  notifier.close();
  const refused = notifier.show(notice("n-2"));
  notifier.close();
  assert.equal(await refused, false);
  const again = notifier.show(notice("n-2"));
  made[3].webContents.emit("did-finish-load");
  assert.equal(await again, true);
});

test("the stack follows the corner when displays change and stops listening on close", () => {
  const screen = new EventEmitter();
  let area = { x: 0, y: 0, width: 1920, height: 1040 };
  screen.getPrimaryDisplay = () => ({ workArea: area });
  const { made, electron } = fakeElectron();
  electron.screen = screen;
  const notifier = new MailNotifier({ electron, page: "file:///mail-toast.html", preload: "p.js" });
  notifier.show(notice("n-1"));
  const popup = made[0];
  popup.webContents.emit("did-finish-load");
  notifier.resize(popup.webContents, 100);
  assert.equal(popup.bounds.x, 1920 - TOAST_WIDTH - MARGIN);
  // The laptop is undocked: a smaller primary display at another origin.
  area = { x: -1280, y: 0, width: 1280, height: 720 };
  screen.emit("display-removed");
  assert.deepEqual(popup.bounds, { x: -1280 + 1280 - TOAST_WIDTH - MARGIN, y: 720 - 100 - MARGIN, width: TOAST_WIDTH, height: 100 });
  area = { x: 0, y: 0, width: 2560, height: 1400 };
  screen.emit("display-metrics-changed");
  assert.equal(popup.bounds.x, 2560 - TOAST_WIDTH - MARGIN);
  // A hidden, empty stack is not moved on screen.
  notifier.resize(popup.webContents, 0);
  const hiddenBounds = popup.bounds;
  area = { x: 0, y: 0, width: 800, height: 600 };
  screen.emit("display-metrics-changed");
  assert.equal(popup.bounds, hiddenBounds);
  notifier.close();
  for (const name of ["display-metrics-changed", "display-removed", "display-added"]) {
    assert.equal(screen.listenerCount(name), 0, name);
  }
});

test("a click that arrives while the main page reloads is kept until the page takes it", () => {
  const main = fs.readFileSync(path.join(__dirname, "main.js"), "utf8");
  const preload = fs.readFileSync(path.join(__dirname, "preload.js"), "utf8");
  assert.match(main, /pendingMailActivation = \{ target, at: Date\.now\(\), origin: pageOrigin\(window\.webContents\.getURL\(\)\) \};\s*window\.webContents\.send\("harness:mailNotificationActivated"/);
  const handler = main.slice(main.indexOf('ipcMain.handle("harness:takeMailNotificationActivation"'));
  assert.ok(handler.length > 0);
  assert.match(handler.slice(0, 400), /if \(!fromHarnessWindow\(event\)\) return null;/);
  assert.match(handler.slice(0, 400), /pendingMailActivation = null;/, "one click opens one email, once");
  const code = handler.slice(0, handler.indexOf('ipcMain.on("mail-toast:resize"'));
  let take = null;
  const helper = main.slice(main.indexOf("function pageOrigin"), main.indexOf("const mailNotifier = new MailNotifier"));
  const context = { ipcMain: { handle: (_name, callback) => { take = callback; } }, fromHarnessWindow: (event) => event.trusted,
    MAIL_ACTIVATION_MS: 120000, Date: { now: () => 1000 }, URL, String, pendingMailActivation: null };
  require("node:vm").runInNewContext(helper + code, context);
  const page = (url, trusted = true) => ({ trusted, sender: { getURL: () => url } });
  const projectA = "http://127.0.0.1:51001/?token=a";
  const target = { account_id: "acct1", message_id: "msg1", draft_id: "draft1" };
  context.pendingMailActivation = { target, at: 1000, origin: "http://127.0.0.1:51001" };
  assert.equal(take(page(projectA, false)), null, "another page cannot take the click");
  assert.deepEqual(take(page(projectA)), target, "the reloaded Nexus page receives it");
  assert.equal(take(page(projectA)), null, "and only once");
  context.pendingMailActivation = { target, at: 1000 - 120000, origin: "http://127.0.0.1:51001" };
  assert.equal(take(page(projectA)), null, "a click from minutes ago does not open mail later");
  // A card from project A clicked while project B starts belongs to A only.
  context.pendingMailActivation = { target, at: 1000, origin: "http://127.0.0.1:51001" };
  assert.equal(take(page("http://127.0.0.1:52002/?token=b")), null, "another project's page never opens it");
  context.pendingMailActivation = { target, at: 1000, origin: "file://" };
  assert.equal(take(page("file:///C:/app/pages/starting.html")), null, "nor does a local starting page");
  // Opening another project drops the held click and the pop-up's cards.
  const opening = main.slice(main.indexOf("async function openProject"), main.indexOf('showPage("starting.html"'));
  assert.match(opening, /mailNotifier\.close\(\);/);
  const showing = main.slice(main.indexOf("function showPage"), main.indexOf("function reportServerStopped"));
  assert.match(showing, /pendingMailActivation = null;/);
  // The page asks when it starts listening and again on every poke.
  const bridge = preload.slice(preload.indexOf("onMailNotificationActivated"), preload.indexOf("onFullScreenChanged"));
  assert.match(bridge, /ipcRenderer\.invoke\("harness:takeMailNotificationActivation"\)/);
  assert.match(bridge, /ipcRenderer\.on\("harness:mailNotificationActivated", take\);\s*take\(\);/);
});
