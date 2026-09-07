"use strict";

const assert = require("node:assert/strict");
const {randomBytes} = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const {chromium} = require("playwright-core");

const ui = path.resolve(__dirname, "../src/our_harness/ui");
const runtime = path.join(__dirname, "build-output/win-unpacked/resources/runtime");
const manifestPath = path.join(runtime, "NEXUS_RUNTIME.json");
const source = fs.readFileSync(path.join(ui, "app.js"), "utf8");
function section(start, end, from = 0) {
  const begin = source.indexOf(start, from);
  const finish = source.indexOf(end, begin);
  assert.ok(begin >= 0 && finish > begin, start);
  return source.slice(begin, finish);
}

// Exercise actual navigation wiring, view switching, session establishment,
// and boot ordering. Unrelated panel renderers have no work in this fixture.
const navigation = section('  document.querySelectorAll("[data-view]")',
  '  $("canvas").addEventListener("dragover"', source.indexOf("function bindEvents"));
const implementation = `
  const $ = id => document.getElementById(id);
  let token = '', sessionBootstrap = null, startedId = '', nexusProjectName = '';
  let template, graph, catalog, nextId, focusedNodeId;
  window.errors = [];
  function showError(message) { window.errors.push(message); }
  function announce() {}
  function bindEvents() { ${navigation} }
  ${section("function bootstrapSession()", "// Asking somebody for one line")}
  ${section("let userViewSelectionRevision", "/* ---- Start here:")}
  ${section("async function boot()", "// ---- the board of agents")}
  function migrateGraph(value) { return value; }
  async function refreshSwarm() { try { await request('/api/swarm'); } catch (error) { showError(error.message); } }
  async function refreshDirectLongGoalRecoveries() { return {hasPending: true}; }
  ${["loadNexusAppIcon", "startWebChatBridge", "render", "renderTeamNotes", "renderWhatItIsDoing",
    "refreshProjects", "validate", "refreshUsage", "loadWhatCanBeDoneForYou", "refreshCheckup",
    "refreshHowItWorks", "refreshChecks", "restoreAuthorityRepairSuccess", "refreshTeamNotes",
    "refreshWorkflows", "pollEvents", "refreshSettings", "refreshWebChatSettingsRecoveryStatus"]
    .map(name => `async function ${name}() {}`).join("\n")}
  window.bootDone = false;
  void boot().then(() => { window.bootDone = true; });
`;

for (const input of ["pointer", "keyboard"]) {
  for (const bootstrapFails of [false, true]) {
    test(`${input} navigation waits for its handler and survives delayed startup (bootstrap failure: ${bootstrapFails})`, {
      skip: !fs.existsSync(manifestPath) && !process.env.NEXUS_TEST_CHROMIUM,
      timeout: 45000,
    }, async () => {
      const manifest = fs.existsSync(manifestPath) ? JSON.parse(fs.readFileSync(manifestPath, "utf8")) : {};
      const executablePath = process.env.NEXUS_TEST_CHROMIUM || path.join(runtime, "playwright", manifest.playwright.chromium_executable);
      const browser = await chromium.launch({executablePath, headless: true});
      let releaseScript;
      let releaseBootstrap;
      const scriptReady = new Promise(resolve => { releaseScript = resolve; });
      const bootstrapReady = new Promise(resolve => { releaseBootstrap = resolve; });
      const sessionValue = randomBytes(24).toString("hex");
      const requests = [];
      const pageErrors = [];
      try {
        const page = await browser.newPage();
        page.on("pageerror", error => pageErrors.push(error.message));
        await page.route("http://nexus-navigation.test/**", async route => {
          const pathname = new URL(route.request().url()).pathname;
          if (pathname === "/") return route.fulfill({contentType: "text/html", body: fs.readFileSync(path.join(ui, "index.html"), "utf8")});
          if (pathname === "/styles.css") return route.fulfill({contentType: "text/css", body: fs.readFileSync(path.join(ui, "styles.css"), "utf8")});
          if (pathname === "/app.js") {
            await scriptReady;
            return route.fulfill({contentType: "application/javascript", body: implementation});
          }
          requests.push({path: pathname, method: route.request().method(), headers: route.request().headers()});
          if (pathname === "/api/bootstrap") {
            await bootstrapReady;
            return route.fulfill({status: bootstrapFails ? 503 : 200, json: bootstrapFails
              ? {error: "The local service is unavailable"}
              : {token: sessionValue, project: "Portable project", template: {nodes: [], edges: []}}});
          }
          return route.fulfill({json: {}});
        });
        await page.goto("http://nexus-navigation.test/", {waitUntil: "commit"});
        const swarm = page.locator('[data-view="swarm"]');
        await swarm.waitFor({state: "visible"});
        assert.equal(await page.locator("[data-view]").evaluateAll(buttons => buttons.every(button => button.disabled)), true);
        assert.equal(await page.locator("#swarmView").isVisible(), false);
        let clicking;
        if (input === "pointer") clicking = swarm.click();
        else {
          await swarm.focus();
          await page.keyboard.press("Enter");
          assert.equal(await page.locator("#swarmView").isVisible(), false);
        }
        releaseScript();
        await page.waitForFunction(() => !document.querySelector('[data-view="swarm"]').disabled);
        if (input === "pointer") await clicking;
        else { await swarm.focus(); await page.keyboard.press("Enter"); }
        assert.equal(await page.locator("#swarmView").isVisible(), true);
        assert.equal(await page.evaluate(() => window.bootDone), false);
        assert.deepEqual(requests.map(one => one.path), ["/api/bootstrap"]);
        const settings = page.locator('[data-view="settings"]');
        if (input === "pointer") await settings.click();
        else { await settings.focus(); await page.keyboard.press("Enter"); }
        releaseBootstrap();
        await page.waitForFunction(() => window.bootDone);
        assert.equal(await page.locator("#settingsView").isVisible(), true,
          "late recovery inventory or a startup error must preserve the latest chosen view");
        assert.equal(await page.locator("#swarmView").isVisible(), false);
        assert.equal(await settings.getAttribute("aria-pressed"), "true");
        assert.equal(await page.locator("#skipToWorkspace").getAttribute("href"), "#settingsView");
        if (bootstrapFails) {
          assert.deepEqual(requests.map(one => one.path), ["/api/bootstrap"]);
          assert.ok((await page.evaluate(() => window.errors)).some(message => message.includes("local service")));
        } else {
          assert.equal(requests.filter(one => one.path === "/api/bootstrap").length, 1);
          assert.equal(requests.filter(one => one.path === "/api/swarm").length, 1);
          assert.ok(requests.filter(one => one.path !== "/api/bootstrap")
            .every(one => one.headers["x-harness-token"] === sessionValue));
          assert.deepEqual(await page.evaluate(() => window.errors), []);
        }
        assert.ok(requests.every(one => one.method === "GET"), "startup navigation must never dispatch provider work");
        assert.deepEqual(pageErrors, []);
      } finally {
        releaseScript();
        releaseBootstrap();
        await browser.close();
      }
    });
  }
}
