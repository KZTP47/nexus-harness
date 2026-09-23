"use strict";

// Corner notifications for new mail the assistant is drafting a reply to,
// shaped like the "your friend is now playing" pop-ups in Steam. One small
// always-on-top window in the bottom-right corner of the primary display holds
// the stack. It is never focusable, so it cannot take the keyboard away from
// whatever somebody is typing in another app; a click still opens the mail.

const TOAST_WIDTH = 368;
const MARGIN = 16;
const MAX_HEIGHT = 480;
const ID_PATTERN = /^[A-Za-z0-9:_-]{1,128}$/;
// The page shows at most three cards, so only the newest few need to wait
// for it to load.
const MOST_WAITING = 3;
// After this many pop-ups in a row broke before showing anything, report
// failure so the page shows its own in-page card instead.
const MOST_BROKEN = 2;

function clean(value, limit) {
  return String(value ?? "")
    .replace(/[\u0000-\u001f\u007f\u2028\u2029]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, limit);
}

// Everything that reaches the pop-up passes through here. The renderer words
// the card (one place, shared with the in-page fallback); the main process
// only accepts plain, bounded text and identifiers, never markup or a place
// to go.
function sanitizeNotice(raw) {
  if (!raw || typeof raw !== "object") return null;
  const id = String(raw.id ?? "");
  if (!ID_PATTERN.test(id)) return null;
  const target = {};
  for (const key of ["account_id", "message_id", "draft_id"]) {
    const value = String(raw[key] ?? "");
    target[key] = ID_PATTERN.test(value) ? value : "";
  }
  const title = clean(raw.title, 200);
  if (!title) return null;
  return {
    id,
    title,
    action: clean(raw.action, 160),
    detail: clean(raw.detail, 300),
    avatar: [...clean(raw.avatar, 8)].slice(0, 2).join("") || "@",
    ...target,
  };
}

class MailNotifier {
  constructor(options = {}) {
    this.electron = options.electron;
    this.page = options.page;
    this.preload = options.preload;
    this.onActivate = typeof options.onActivate === "function" ? options.onActivate : () => {};
    this.guard = typeof options.guard === "function" ? options.guard : () => {};
    this.window = null;
    this.loaded = false;
    this.waiting = [];
    this.shown = new Map();
    this.broken = 0;
  }

  owns(sender) {
    return Boolean(this.window && !this.window.isDestroyed() && sender === this.window.webContents);
  }

  show(raw) {
    const notice = sanitizeNotice(raw);
    if (!notice) return false;
    if (this.broken >= MOST_BROKEN) return false;
    this.shown.set(notice.id, notice);
    // Remember only what is still on screen or about to be.
    while (this.shown.size > 50) this.shown.delete(this.shown.keys().next().value);
    const target = this.ensureWindow();
    if (!target) return false;
    if (this.loaded) target.webContents.send("mail-toast:show", notice);
    else {
      this.waiting.push(notice);
      this.waiting.splice(0, Math.max(0, this.waiting.length - MOST_WAITING));
    }
    return true;
  }

  ensureWindow() {
    if (this.window && !this.window.isDestroyed()) return this.window;
    const { BrowserWindow } = this.electron || {};
    if (typeof BrowserWindow !== "function" || !this.page) return null;
    const created = new BrowserWindow({
      width: TOAST_WIDTH,
      height: 120,
      show: false,
      frame: false,
      transparent: true,
      backgroundColor: "#00000000",
      resizable: false,
      movable: false,
      minimizable: false,
      maximizable: false,
      fullscreenable: false,
      closable: true,
      skipTaskbar: true,
      alwaysOnTop: true,
      focusable: false,
      hasShadow: false,
      title: "Nexus Harness notification",
      webPreferences: {
        preload: this.preload,
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        webviewTag: false,
        spellcheck: false,
        backgroundThrottling: false,
      },
    });
    this.window = created;
    this.loaded = false;
    if (typeof created.setAlwaysOnTop === "function") created.setAlwaysOnTop(true, "screen-saver");
    if (typeof created.setVisibleOnAllWorkspaces === "function") {
      created.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
    }
    this.guard(created.webContents);
    created.webContents.once("did-finish-load", () => {
      if (this.window !== created) return;
      this.loaded = true;
      this.broken = 0;
      for (const notice of this.waiting.splice(0)) created.webContents.send("mail-toast:show", notice);
    });
    created.on("closed", () => {
      if (this.window === created) {
        this.window = null;
        this.loaded = false;
      }
    });
    // A pop-up whose page crashed, hangs or never loaded would otherwise sit in
    // the corner for the rest of the session and swallow every later notice.
    // Drop it so the next notice builds a fresh one.
    const discard = () => this.discard(created);
    created.webContents.on("render-process-gone", discard);
    created.webContents.on("unresponsive", discard);
    created.webContents.on("did-fail-load", (_event, _code, _description, _url, isMainFrame) => {
      if (isMainFrame !== false) discard();
    });
    // Destroying the pop-up while its page loads rejects the load; that is expected.
    Promise.resolve(created.loadURL(this.page)).catch(() => {});
    return created;
  }

  discard(created) {
    if (this.window !== created) return;
    if (!this.loaded) this.broken += 1;
    this.window = null;
    this.loaded = false;
    this.waiting = [];
    if (!created.isDestroyed()) created.destroy();
  }

  // The page reports how tall its stack is. The window grows upward from the
  // corner so the newest card always sits nearest the edge of the screen.
  resize(sender, height) {
    if (!this.owns(sender)) return false;
    const wanted = Math.min(MAX_HEIGHT, Math.max(0, Math.ceil(Number(height) || 0)));
    if (!wanted) {
      this.window.hide();
      return true;
    }
    const area = this.workArea();
    this.window.setBounds({
      x: Math.round(area.x + area.width - TOAST_WIDTH - MARGIN),
      y: Math.round(area.y + area.height - wanted - MARGIN),
      width: TOAST_WIDTH,
      height: wanted,
    });
    if (!this.window.isVisible()) {
      if (typeof this.window.showInactive === "function") this.window.showInactive();
      else this.window.show();
    }
    return true;
  }

  workArea() {
    try {
      const area = this.electron.screen.getPrimaryDisplay().workArea;
      if (area && Number.isFinite(area.width) && Number.isFinite(area.height)) return area;
    } catch (_error) { /* fall back to a plain corner */ }
    return { x: 0, y: 0, width: 1280, height: 800 };
  }

  activate(sender, id) {
    if (!this.owns(sender)) return false;
    const notice = this.shown.get(String(id || ""));
    if (!notice) return false;
    this.onActivate({
      account_id: notice.account_id, message_id: notice.message_id, draft_id: notice.draft_id,
    });
    return true;
  }

  close() {
    this.waiting = [];
    this.shown.clear();
    if (this.window && !this.window.isDestroyed()) this.window.destroy();
    this.window = null;
    this.loaded = false;
  }
}

module.exports = { MailNotifier, sanitizeNotice, TOAST_WIDTH, MARGIN };
