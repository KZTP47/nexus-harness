"use strict";

const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");
const {spawn} = require("node:child_process");
const {EventEmitter} = require("node:events");

const wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

function browserCandidates(environment = process.env) {
  const local = environment.LOCALAPPDATA || "";
  const systemRoot = (environment.SystemDrive || "C:") + path.win32.sep;
  const program = environment.ProgramFiles || environment.PROGRAMFILES
    || path.win32.join(systemRoot, "Program Files");
  const program32 = environment["ProgramFiles(x86)"] || environment.PROGRAMFILES_X86
    || path.win32.join(systemRoot, "Program Files (x86)");
  return [
    {family: "chrome", executable: path.join(program, "Google", "Chrome", "Application", "chrome.exe")},
    {family: "chrome", executable: path.join(program32, "Google", "Chrome", "Application", "chrome.exe")},
    {family: "chrome", executable: path.join(local, "Google", "Chrome", "Application", "chrome.exe")},
    {family: "edge", executable: path.join(program32, "Microsoft", "Edge", "Application", "msedge.exe")},
    {family: "edge", executable: path.join(program, "Microsoft", "Edge", "Application", "msedge.exe")},
    {family: "edge", executable: path.join(local, "Microsoft", "Edge", "Application", "msedge.exe")},
  ];
}

function findInstalledBrowser(preferred = ["chrome", "edge"], options = {}) {
  const exists = options.exists || fs.existsSync;
  const candidates = options.candidates || browserCandidates(options.environment);
  for (const family of preferred) {
    const found = candidates.find((one) => one.family === family && exists(one.executable));
    if (found) return found;
  }
  return null;
}

async function reserveLoopbackPort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.once("error", reject);
    server.listen({host: "127.0.0.1", port: 0, exclusive: true}, () => {
      const address = server.address();
      server.close((error) => error ? reject(error) : resolve(address.port));
    });
  });
}

class ExternalPageContents extends EventEmitter {
  constructor(transport, initialUrl) {
    super();
    this.transport = transport;
    this.url = String(initialUrl || "about:blank");
    this.title = transport.provider.label;
    this.closed = false;
    this.loading = true;
    this.page = null;
    this.boundPages = new WeakSet();
    this.ready = transport.openPage(this.url).then(async (page) => {
      if (this.closed) {
        await page.close().catch(() => {});
        throw new Error(`${transport.provider.label}'s browser page was closed`);
      }
      await this.bindPage(page);
      this.emit("did-finish-load");
      return page;
    }).catch((error) => {
      this.loading = false;
      this.emit("did-fail-load", {}, -2, error.message, this.url, true);
      throw error;
    });
  }

  isDestroyed() { return this.closed; }
  isLoading() { return this.loading; }
  getURL() { return this.url; }
  getTitle() { return this.title; }

  async bindPage(page) {
    if (!page || page.isClosed?.()) throw new Error(
      `${this.transport.provider.label}'s selected browser tab is no longer open`);
    this.page = page;
    this.url = page.url() || this.url;
    this.title = (await page.title().catch(() => "")) || this.title;
    this.loading = false;
    if (this.boundPages.has(page)) return page;
    this.boundPages.add(page);
    page.on("framenavigated", async (frame) => {
      if (this.page !== page || frame !== page.mainFrame()) return;
      this.url = frame.url() || this.url;
      this.title = (await page.title().catch(() => "")) || this.title;
      this.emit("did-navigate", {}, this.url, 200, "OK");
      this.emit("page-title-updated", {}, this.title);
    });
    page.on("close", () => {
      if (this.page !== page || this.closed) return;
      // OAuth and provider SPAs can finish sign-in in a replacement tab and
      // close the original one. Keep this logical contents recoverable; the
      // next operation adopts the currently selected provider tab.
      this.page = null;
      this.loading = false;
    });
    return page;
  }

  async pageForOperation({preferCurrent = false} = {}) {
    await this.ready;
    if (this.closed) throw new Error(`${this.transport.provider.label}'s browser page was closed`);
    if (!preferCurrent && this.page && !this.page.isClosed()) return this.page;
    const page = await this.transport.currentProviderPage();
    if (!page) throw new Error(
      `Nexus could not find an open ${this.transport.provider.label} chat tab in its secure browser window.`);
    return this.bindPage(page);
  }

  async useCurrentPage() {
    const page = await this.pageForOperation({preferCurrent: true});
    this.url = page.url() || this.url;
    this.title = (await page.title().catch(() => "")) || this.title;
    return {url: this.url, title: this.title};
  }

  async loadURL(url) {
    this.url = String(url || "about:blank");
    this.loading = true;
    const page = await this.pageForOperation();
    try {
      await page.goto(this.url, {waitUntil: "domcontentloaded", timeout: 90000});
      this.url = page.url() || this.url;
      this.title = (await page.title().catch(() => "")) || this.title;
      this.emit("did-navigate", {}, this.url, 200, "OK");
      return this.url;
    } finally {
      this.loading = false;
      this.emit("did-finish-load");
    }
  }

  async executeJavaScript(script) {
    let lastError = null;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const page = await this.pageForOperation();
      try {
        return await page.evaluate(String(script));
      } catch (error) {
        lastError = error;
        if (!/execution context was destroyed|cannot find context|target page.*closed/i.test(
          String(error?.message || error))) throw error;
        if (page.isClosed?.()) this.page = null;
        await wait(100);
      }
    }
    throw lastError;
  }

  async pressEnter() {
    const page = await this.pageForOperation();
    await page.keyboard.press("Enter");
  }

  async replaceTextAndSubmit(text, selectors = {}) {
    const page = await this.pageForOperation();
    const selected = await page.evaluate((contract) => {
      const visible = (one) => {
        if (!one) return false;
        const style = getComputedStyle(one);
        const rect = one.getBoundingClientRect();
        return style.visibility !== "hidden" && style.display !== "none"
          && rect.width > 1 && rect.height > 1;
      };
      const composer = (contract.composer || []).flatMap(
        (selector) => [...document.querySelectorAll(selector)]).find(visible);
      if (!composer) return false;
      composer.focus();
      if (composer instanceof HTMLTextAreaElement || composer instanceof HTMLInputElement) {
        composer.setSelectionRange(0, composer.value.length);
        return true;
      }
      const selection = getSelection();
      if (!selection) return false;
      const range = document.createRange();
      range.selectNodeContents(composer);
      selection.removeAllRanges();
      selection.addRange(range);
      return true;
    }, selectors);
    if (!selected) return {
      activated: false, failureCode: "composer_selection_failed",
      activationMethod: "none",
    };
    await page.keyboard.insertText(String(text || ""));
    let previous = null;
    let target = null;
    for (let attempt = 0; attempt < 50; attempt += 1) {
      await wait(100);
      const candidate = await page.evaluate(({selectors: contract, expected}) => {
        const visible = (one) => {
          if (!one) return false;
          const style = getComputedStyle(one);
          const rect = one.getBoundingClientRect();
          return style.visibility !== "hidden" && style.display !== "none"
            && rect.width > 1 && rect.height > 1;
        };
        const composer = (contract.composer || []).flatMap(
          (selector) => [...document.querySelectorAll(selector)]).find(visible);
        const normal = (value) => String(value || "").replace(/\s+/g, " ").trim();
        const actual = normal(
          composer instanceof HTMLTextAreaElement || composer instanceof HTMLInputElement
            ? composer.value : (composer?.innerText || composer?.textContent || ""));
        if (!composer || actual !== normal(expected)) return null;
        let scope = composer;
        let found = null;
        for (let depth = 0; scope && depth < 10 && !found; depth += 1) {
          const candidates = [...new Set((contract.send || []).flatMap(
            (selector) => [...scope.querySelectorAll(selector)]).filter(visible))];
          if (candidates.length === 1) found = candidates[0];
          scope = scope.parentElement;
        }
        const button = found?.matches("button, [role='button']")
          ? found : found?.querySelector("button, [role='button']") || found;
        if (!button || !visible(button)) return null;
        if (button.disabled || button.getAttribute("aria-disabled") === "true") return null;
        const rect = button.getBoundingClientRect();
        const topmost = document.elementsFromPoint(
          rect.left + rect.width / 2, rect.top + rect.height / 2);
        if (!topmost.some((one) => one === button || button.contains(one))) return null;
        return {
          x: rect.left + rect.width / 2, y: rect.top + rect.height / 2,
          fingerprint: [
            button.tagName, button.id, button.getAttribute("data-testid"),
            button.getAttribute("data-test-id"), String(button.className || ""),
          ].join("|"),
        };
      }, {selectors, expected: String(text || "")});
      if (candidate && previous
          && candidate.fingerprint === previous.fingerprint
          && Math.abs(candidate.x - previous.x) < 1
          && Math.abs(candidate.y - previous.y) < 1) {
        target = candidate;
        break;
      }
      previous = candidate;
    }
    if (!target) return {
      activated: false, failureCode: "submit_control_unavailable",
      activationMethod: "none",
    };
    await page.mouse.click(target.x, target.y);
    return {activated: true, sendActivated: true, activationMethod: "trusted_pointer"};
  }

  async setFiles(files) {
    const page = await this.pageForOperation();
    const input = page.locator("input[type=file]").last();
    await input.waitFor({state: "attached", timeout: 5000});
    await input.setInputFiles(files);
  }

  async bringToFront() {
    const page = await this.pageForOperation();
    await page.bringToFront();
  }

  close() {
    this.closed = true;
    if (this.page && !this.page.isClosed()) this.page.close().catch(() => {});
  }
}

class ExternalBrowserTransport {
  constructor(options) {
    this.provider = options.provider;
    this.profilePath = options.profilePath;
    this.preferred = options.preferred || ["chrome", "edge"];
    this.findBrowser = options.findBrowser || findInstalledBrowser;
    this.spawn = options.spawn || spawn;
    this.reservePort = options.reservePort || reserveLoopbackPort;
    this.ensureDirectory = options.ensureDirectory || ((directory) => fs.mkdirSync(directory, {recursive: true}));
    this.chromium = options.chromium || null;
    this.browser = null;
    this.context = null;
    this.process = null;
    this.port = 0;
    this.starting = null;
    this.initialPage = null;
    this.initialClaimed = false;
    this.pages = new Set();
  }

  async playwright() {
    if (!this.chromium) this.chromium = require("playwright-core").chromium;
    return this.chromium;
  }

  async start(initialUrl) {
    if (this.context && this.browser?.isConnected()) return this.context;
    if (this.starting) return this.starting;
    this.starting = (async () => {
      const selected = this.findBrowser(this.preferred);
      if (!selected) throw new Error(
        `${this.provider.label} needs Google Chrome or Microsoft Edge for its secure sign-in, but neither browser was found.`);
      this.ensureDirectory(this.profilePath);
      this.port = await this.reservePort();
      const args = [
        `--user-data-dir=${this.profilePath}`,
        `--remote-debugging-port=${this.port}`,
        "--remote-debugging-address=127.0.0.1",
        "--no-first-run", "--no-default-browser-check", "--disable-session-crashed-bubble",
        "--new-window", String(initialUrl || this.provider.home),
      ];
      // Do not use --enable-automation, --headless, or --remote-debugging-port=0.
      // Claude's Cloudflare policy rejects those identities. A fixed loopback
      // endpoint lets Nexus attach after an ordinary browser has started while
      // navigator.webdriver remains false.
      this.process = this.spawn(selected.executable, args, {
        stdio: "ignore", windowsHide: false,
      });
      const engine = await this.playwright();
      let lastError = null;
      for (let attempt = 0; attempt < 120; attempt += 1) {
        if (this.process.exitCode != null) throw new Error(
          `${selected.family === "edge" ? "Microsoft Edge" : "Google Chrome"} closed before ${this.provider.label} opened.`);
        try {
          this.browser = await engine.connectOverCDP(`http://127.0.0.1:${this.port}`);
          break;
        } catch (error) {
          lastError = error;
          await wait(250);
        }
      }
      if (!this.browser) throw new Error(
        `Nexus could not connect to the ${this.provider.label} browser window: ${lastError?.message || "startup timed out"}`);
      this.context = this.browser.contexts()[0];
      this.initialPage = this.context.pages().find((page) => (
        page.url() === "about:blank" || page.url().includes(new URL(initialUrl).hostname)
      )) || null;
      this.browser.on("disconnected", () => {
        this.browser = null;
        this.context = null;
        this.initialPage = null;
        for (const contents of this.pages) {
          contents.closed = true;
          contents.emit("destroyed");
        }
        this.pages.clear();
      });
      return this.context;
    })().finally(() => { this.starting = null; });
    return this.starting;
  }

  createContents(initialUrl) {
    const contents = new ExternalPageContents(this, initialUrl);
    this.pages.add(contents);
    contents.once("destroyed", () => this.pages.delete(contents));
    return contents;
  }

  async openPage(initialUrl) {
    const context = await this.start(initialUrl);
    let page = null;
    if (!this.initialClaimed && this.initialPage && !this.initialPage.isClosed()) {
      this.initialClaimed = true;
      page = this.initialPage;
    } else {
      page = await context.newPage();
      await page.goto(initialUrl, {waitUntil: "domcontentloaded", timeout: 90000});
    }
    // An installed browser can reuse or restore the Nexus profile's generic
    // provider tab instead of honoring the requested saved-conversation URL.
    // Always put the first claimed tab on the exact logical thread requested
    // by this contents object before it is allowed to submit anything.
    const savedConversation = initialUrl !== this.provider.home
      && initialUrl !== this.provider.newChat;
    if (page.url() === "about:blank" || (savedConversation && page.url() !== initialUrl)) {
      await page.goto(initialUrl, {waitUntil: "domcontentloaded", timeout: 90000});
    }
    await page.bringToFront();
    return page;
  }

  async currentProviderPage() {
    const context = await this.start(this.provider.home);
    const candidates = context.pages().filter((page) => {
      if (!page || page.isClosed()) return false;
      try {
        const parsed = new URL(page.url());
        return parsed.protocol === "https:" && (this.provider.hosts || []).some(
          (host) => parsed.hostname === host || parsed.hostname.endsWith(`.${host}`));
      } catch (_error) {
        return false;
      }
    });
    let best = null;
    let bestScore = -1;
    for (let index = 0; index < candidates.length; index += 1) {
      const page = candidates[index];
      const state = await page.evaluate(() => ({
        focused: document.hasFocus(), visibility: document.visibilityState,
      })).catch(() => ({focused: false, visibility: "hidden"}));
      const url = page.url();
      const providerHome = String(this.provider.home || "").replace(/\/+$/, "");
      const score = (state.focused ? 8 : 0) + (state.visibility === "visible" ? 4 : 0)
        + (url.replace(/\/+$/, "") !== providerHome ? 2 : 0) + index / 1000;
      if (score >= bestScore) {
        best = page;
        bestScore = score;
      }
    }
    return best;
  }

  async close() {
    for (const contents of [...this.pages]) contents.close();
    this.pages.clear();
    // Kill immediately as well as closing CDP so an app shutdown cannot leave
    // a controlled browser process behind while Electron's event loop exits.
    if (this.process && this.process.exitCode == null) this.process.kill();
    if (this.browser) await this.browser.close().catch(() => {});
    this.browser = null;
    this.context = null;
    this.process = null;
  }
}

module.exports = {
  ExternalBrowserTransport, ExternalPageContents,
  browserCandidates, findInstalledBrowser, reserveLoopbackPort,
};
