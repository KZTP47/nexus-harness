"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const path = require("node:path");
const { MailNotifier, sanitizeNotice, TOAST_WIDTH, MARGIN } = require("./mail-notifier");

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

test("the pop-up waits for its page, sits in the corner and never takes focus", () => {
  const { made, electron } = fakeElectron({ x: 0, y: 0, width: 1920, height: 1040 });
  let guarded = 0;
  const notifier = new MailNotifier({ electron, page: "file:///mail-toast.html", preload: "p.js", guard: () => { guarded += 1; } });
  assert.equal(notifier.show(notice("n-1")), true);
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
  notifier.show(notice("n-2"));
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

test("a crashed pop-up is replaced by a fresh one instead of swallowing later notices", () => {
  const { made, electron } = fakeElectron();
  const notifier = new MailNotifier({ electron, page: "file:///mail-toast.html", preload: "p.js" });
  notifier.show(notice("n-1"));
  made[0].webContents.emit("did-finish-load");
  made[0].visible = true;
  made[0].webContents.emit("render-process-gone", {}, { reason: "crashed" });
  assert.equal(made[0].destroyed, true, "the dead always-on-top window does not stay in the corner");
  assert.equal(notifier.show(notice("n-2")), true);
  assert.equal(made.length, 2, "the next notice builds a new pop-up");
  made[1].webContents.emit("did-finish-load");
  assert.deepEqual(made[1].webContents.sent.map(([, value]) => value.id), ["n-2"]);
  // A hung page is treated the same way.
  made[1].webContents.emit("unresponsive");
  assert.equal(made[1].destroyed, true);
});

test("a pop-up page that never loads keeps only the newest notices and then lets the page show its own", () => {
  const { made, electron } = fakeElectron();
  const notifier = new MailNotifier({ electron, page: "file:///mail-toast.html", preload: "p.js" });
  for (let n = 1; n <= 1000; n += 1) notifier.show(notice("n-" + n));
  assert.deepEqual(notifier.waiting.map(item => item.id), ["n-998", "n-999", "n-1000"], "the wait queue is bounded");
  // A subframe failure is not the pop-up page failing.
  made[0].webContents.emit("did-fail-load", {}, -2, "failed", "about:blank", false);
  assert.equal(made[0].destroyed, false);
  made[0].webContents.emit("did-fail-load", {}, -6, "file not found", "file:///mail-toast.html", true);
  assert.equal(made[0].destroyed, true);
  assert.deepEqual(notifier.waiting, []);
  assert.equal(notifier.show(notice("n-1001")), true, "one more attempt with a fresh pop-up");
  made[1].webContents.emit("did-fail-load", {}, -6, "file not found", "file:///mail-toast.html", true);
  assert.equal(notifier.show(notice("n-1002")), false, "after repeated failures the caller falls back to its in-page card");
  assert.equal(made.length, 2, "no endless stream of broken windows");
});

test("a pop-up that loaded once and later crashed is still retried", () => {
  const { made, electron } = fakeElectron();
  const notifier = new MailNotifier({ electron, page: "file:///mail-toast.html", preload: "p.js" });
  for (let round = 0; round < 4; round += 1) {
    assert.equal(notifier.show(notice("n-" + round)), true);
    made[round].webContents.emit("did-finish-load");
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
