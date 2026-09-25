const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const test = require('node:test');
const {chromium} = require('playwright-core');
const ui = path.resolve(__dirname, '../src/our_harness/ui');
const runtime = path.resolve(__dirname, 'build-output/win-unpacked/resources/runtime');
const manifestPath = path.join(runtime, 'NEXUS_RUNTIME.json');
const manifest = fs.existsSync(manifestPath) ? JSON.parse(fs.readFileSync(manifestPath, 'utf8')) : {};
const executablePath = process.env.NEXUS_TEST_CHROMIUM || (manifest.playwright?.chromium_executable && path.join(runtime, 'playwright', manifest.playwright.chromium_executable));
const source = fs.readFileSync(path.join(ui, 'session-health.js'), 'utf8');

const signedOut = {
  key: 'claude-cli:claude', kind: 'claude-cli', label: 'Claude', routes: ['writer'], state: 'signed_out',
  reason: 'Failed to authenticate: OAuth session expired', since: 'x', checked_at: 'x',
  sign_in_opened_at: '2026-09-24T10:00:00Z', sign_in_note: '', manual: false, verifying: false, auto_sign_in: true,
};

async function page(browser, state) {
  const one = await browser.newPage();
  await one.setContent('<header class="topbar">Nexus</header><main>work</main>');
  await one.evaluate(({state}) => {
    window.state = state; window.posts = []; window.cards = [];
    window.harnessDesktop = {showMailNotification: async card => { window.cards.push(card); return true; }};
    window.request = async (url, options) => {
      if (options?.method === 'POST') { window.posts.push(JSON.parse(options.body)); return {}; }
      return structuredClone(window.state);
    };
  }, {state});
  await one.addScriptTag({content: source});
  await one.evaluate(() => window.nexusSessionHealth.start());
  await one.waitForFunction(() => window.nexusSessionBanner);
  return one;
}

test('a lapsed sign-in is shown loudly on every page with take-over controls', {skip: !executablePath, timeout: 45000}, async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const view = await page(browser, {contract: 'nexus-session-health/v1', boot: 'b', seq: 2, settings: {auto_sign_in: true},
      sessions: [signedOut, {...signedOut, key: 'codex-cli:codex', label: 'GPT Codex', state: 'ok'}],
      feed: [{seq: 1, kind: 'signed_out', session: 'claude-cli:claude', title: 'Claude needs you to sign in again', detail: 'Nexus is holding work', age_seconds: 1},
             {seq: 2, kind: 'sign_in_opened', session: 'claude-cli:claude', title: "Nexus opened Claude's sign-in", detail: 'Finish there', age_seconds: 1}]});
    await view.waitForSelector('#nexusSessionBanner:not([hidden]) .nexus-session-problem');
    const text = await view.textContent('#nexusSessionBanner');
    assert.match(text, /Claude needs you to sign in again/);
    assert.match(text, /Nexus opened its sign-in window/);
    assert.doesNotMatch(text, /GPT Codex/, 'healthy sessions stay quiet');
    assert.equal(await view.getAttribute('#nexusSessionBanner', 'role'), 'alert');
    // The banner sits directly under the top bar, above every tab's content.
    assert.equal(await view.evaluate(() => document.querySelector('header.topbar').nextElementSibling.id), 'nexusSessionBanner');
    assert.deepEqual((await view.evaluate(() => window.cards)).map(card => card.title),
      ['Claude needs you to sign in again', "Nexus opened Claude's sign-in"]);

    await view.click('text=Open sign-in again');
    await view.click('text=Check now');
    await view.click('text=I’ll handle it');
    await view.uncheck('.nexus-session-setting input');
    await view.waitForFunction(() => window.posts.length === 4);
    assert.deepEqual(await view.evaluate(() => window.posts), [
      {action: 'open_sign_in', session: 'claude-cli:claude'},
      {action: 'check_now', session: 'claude-cli:claude'},
      {action: 'manual', session: 'claude-cli:claude', manual: true},
      {action: 'settings', auto_sign_in: false},
    ]);

    await view.evaluate(() => {
      window.state = {...window.state, seq: 3, sessions: window.state.sessions.map(one => ({...one, state: 'ok'})),
        feed: [{seq: 3, kind: 'recovered', session: 'claude-cli:claude', title: 'Claude is signed in again', detail: 'continuing', age_seconds: 0}]};
      return window.nexusSessionBanner.tick();
    });
    await view.waitForSelector('.nexus-session-ok');
    assert.match(await view.textContent('#nexusSessionBanner'), /Claude works again/);
    assert.equal(await view.$('.nexus-session-problem'), null);
  } finally {
    await browser.close();
  }
});

test('nothing is shown while every sign-in works, and a manual session says so', {skip: !executablePath, timeout: 45000}, async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const quiet = await page(browser, {boot: 'b', seq: 0, settings: {auto_sign_in: true}, sessions: [{...signedOut, state: 'ok'}], feed: []});
    await quiet.evaluate(() => window.nexusSessionBanner.tick());
    assert.equal(await quiet.getAttribute('#nexusSessionBanner', 'hidden'), '');
    const manual = await page(browser, {boot: 'b', seq: 0, settings: {auto_sign_in: true},
      sessions: [{...signedOut, sign_in_opened_at: '', manual: true, auto_sign_in: false}], feed: []});
    await manual.waitForSelector('.nexus-session-problem');
    const text = await manual.textContent('#nexusSessionBanner');
    assert.match(text, /handle this sign-in yourself/);
    assert.match(text, /Let Nexus handle it/);
  } finally {
    await browser.close();
  }
});
