"use strict";
// Each board agent chooses its own model: route default, catalog choices for
// that route's kind, the saved choice even when unlisted, or a typed name.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const test = require('node:test');
const {chromium} = require('playwright-core');
const ui = process.env.NEXUS_CHAT_UI_ROOT || path.resolve(__dirname, '../src/our_harness/ui');
const runtime = path.resolve(__dirname, 'build-output/win-unpacked/resources/runtime');
const manifestPath = path.join(runtime, 'NEXUS_RUNTIME.json');
const manifest = fs.existsSync(manifestPath) ? JSON.parse(fs.readFileSync(manifestPath, 'utf8')) : {};
const executablePath = process.env.NEXUS_TEST_CHROMIUM || (manifest.playwright?.chromium_executable && path.join(runtime, 'playwright', manifest.playwright.chromium_executable));
const source = fs.readFileSync(path.join(ui, 'app.js'), 'utf8');
const section = (from, to) => source.slice(source.indexOf(from), source.indexOf(to, source.indexOf(from)));

test('the agent panel offers route default, catalog models and a typed model', {skip: !executablePath, timeout: 45000}, async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const page = await browser.newPage();
    const html = fs.readFileSync(path.join(ui, 'index.html'), 'utf8').replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '').replace(/<link\b[^>]*>/gi, '');
    await page.route('http://picker.test/**', route => route.fulfill({contentType: 'text/html', body: html}));
    await page.goto('http://picker.test/');
    await page.addScriptTag({content: `
      const $ = id => document.getElementById(id);
      ${section('function make(tag', 'function migrateGraph')}
      let swarmAgentPictureDraft = '';
      ${section('function fillSwarmAgentModel', 'function renderSwarmAgentSaveState')}
      for (let one = $('swarmAgentModel'); one; one = one.parentElement) one.hidden = false;
      window.claude = {route: 'claude', kind: 'claude-cli', model: 'claude-opus-5', model_choices: [
        {id: 'claude-opus-5', label: 'Claude Opus 5'}, {id: 'claude-sonnet-5', label: 'Claude Sonnet 5'}]};
    `});
    const options = () => page.$$eval('#swarmAgentModel option', all => all.map(one => [one.value, one.textContent]));
    await page.evaluate(() => fillSwarmAgentModel('', claude, 'claude'));
    assert.deepEqual(await options(), [
      ['', 'Route default (claude-opus-5)'], ['claude-opus-5', 'Claude Opus 5 (claude-opus-5)'],
      ['claude-sonnet-5', 'Claude Sonnet 5 (claude-sonnet-5)'], ['__other__', 'Other model (type its name)…']]);
    assert.equal(await page.inputValue('#swarmAgentModel'), '');
    assert.equal(await page.isHidden('#swarmAgentModelOther'), true);
    assert.equal(await page.evaluate(() => agentSettingsFromForm().model), '');
    // A catalog choice is saved as that model.
    await page.locator("#swarmAgentModel").selectOption('claude-sonnet-5');
    assert.equal(await page.evaluate(() => agentSettingsFromForm().model), 'claude-sonnet-5');
    // A typed name.
    await page.evaluate(() => { $('swarmAgentModel').value = '__other__'; $('swarmAgentModelOther').hidden = false; $('swarmAgentModelOther').value = ' my-custom-model '; });
    assert.equal(await page.evaluate(() => agentSettingsFromForm().model), 'my-custom-model');
    // A saved model that the catalog does not list is shown, not silently lost.
    await page.evaluate(() => fillSwarmAgentModel('claude-fable-5-1', claude, 'claude'));
    assert.equal(await page.inputValue('#swarmAgentModel'), '__other__');
    assert.equal(await page.isVisible('#swarmAgentModelOther'), true);
    assert.equal(await page.inputValue('#swarmAgentModelOther'), 'claude-fable-5-1');
    assert.equal(await page.evaluate(() => agentSettingsFromForm().model), 'claude-fable-5-1');
    // No route chosen: only the default and the typed option remain.
    await page.evaluate(() => fillSwarmAgentModel('', null, ''));
    assert.deepEqual((await options()).map(one => one[0]), ['', '__other__']);
  } finally { await browser.close(); }
});

test('done claims show Nexus evidence and the Claude-builds-Codex-reviews preset sets roles', {skip: !executablePath, timeout: 45000}, async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const page = await browser.newPage();
    await page.setContent('<main id="host"></main>');
    await page.addScriptTag({content: `
      const $ = id => document.getElementById(id);
      ${section('function make(tag', 'function migrateGraph')}
      ${section('function locationOpenButton', 'function historicalWorkingFolder')}
      ${section('function appendNexusCompletionCheck', 'function appendChatDeliveryNotice')}
      ${section('function agentRouteKind', 'function appendGoalAccessControls')}
      window.swarmSaid = {who_can_be_used: [{route: 'claude', kind: 'claude-cli'}, {route: 'codex', kind: 'codex-cli'}]};
      function theSwarmAgent() { return null; }
    `});
    await page.evaluate(() => appendNexusCompletionCheck($('host'), {correlation: {
      nexus_check_verdict: 'Opened from disk (how the user opens it): PROBLEM - 2 script errors, the page looks blank.',
      nexus_check_page: 'index.html', nexus_check_screenshot: 'C:/project/.harness/previews/index-file.png'}}));
    assert.equal(await page.getAttribute('.chat-nexus-check', 'role'), 'note');
    assert.match(await page.textContent('.chat-nexus-check'), /Nexus checked index\.html: a problem the user would see/);
    assert.equal(await page.isVisible('.chat-nexus-check button:has-text("Open screenshot")'), true);
    await page.evaluate(() => appendNexusCompletionCheck($('host'), {correlation: {}}));
    assert.equal(await page.$$eval('.chat-nexus-check', all => all.length), 1);
    // The preset appears only with a Claude and a Codex agent, and saves fixed roles.
    await page.evaluate(() => {
      window.saved = [];
      appendCollaborationControls($('host'), {}, [{id: 'b', name: 'Builder', who: 'claude'}, {id: 'r', name: 'Checker', who: 'codex'}],
        true, async settings => { window.saved.push(settings); }, true);
    });
    await page.getByRole('button', {name: 'Claude builds, Codex reviews'}).click();
    await page.waitForFunction(() => window.saved.length === 1);
    assert.deepEqual(await page.evaluate(() => window.saved[0]), {mode: 'fixed', writer_id: 'b', reviewer_id: 'r', allow_direct_real_edits: false});
    await page.evaluate(() => { $('host').replaceChildren(); appendCollaborationControls($('host'), {}, [{id: 'b', name: 'B', who: 'claude'}, {id: 'c', name: 'C', who: 'claude'}], true, async () => {}, true); });
    assert.equal(await page.isHidden('button:has-text("Claude builds, Codex reviews")'), true);
  } finally { await browser.close(); }
});
