"use strict";
// Board chats choose their orchestrator: Nexus (the saved conversation) or a
// Live team that streams in the expanded chat. Each chat keeps its own team.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');
const test = require('node:test');
const {chromium} = require('playwright-core');
const ui = process.env.NEXUS_CHAT_UI_ROOT || path.resolve(__dirname, '../src/our_harness/ui');
const runtime = path.resolve(__dirname, 'build-output/win-unpacked/resources/runtime');
const manifestPath = path.join(runtime, 'NEXUS_RUNTIME.json');
const manifest = fs.existsSync(manifestPath) ? JSON.parse(fs.readFileSync(manifestPath, 'utf8')) : {};
const executablePath = process.env.NEXUS_TEST_CHROMIUM || (manifest.playwright?.chromium_executable && path.join(runtime, 'playwright', manifest.playwright.chromium_executable));
const read = name => fs.readFileSync(path.join(ui, name), 'utf8');

const pairChat = {id: 'pair-chat-1', name: 'Chat 1', pair: ['builder', 'reviewer'], project: 'proj',
  pair_agents: [{id: 'builder', name: 'Builder'}, {id: 'reviewer', name: 'Reviewer'}],
  projects: [{id: 'proj', name: 'Portable game', path: 'D:/portable/game'}]};
const otherChat = {...pairChat, id: 'pair-chat-2', name: 'Chat 2'};
const team = {team_id: 'team-1', name: 'Chat 1 · Build a game', project: 'D:/portable/game', lead: 'builder', access: 'ask', mode: 'shared', state: 'running', run: 'r1',
  agents: [{id: 'builder', name: 'Builder', seat: 'builder@team-1', session: {state: 'ready', busy: true}},
           {id: 'reviewer', name: 'Reviewer', seat: 'reviewer@team-1', session: {state: 'ready'}}],
  tasks: [{id: 't', title: 'Build the level', owner: 'builder', state: 'in_progress'}], messages: [], leases: []};

async function open(browser, width = 1264) {
  const page = await browser.newPage({viewport: {width, height: 850}});
  const html = read('index.html').replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '').replace(/<link\b[^>]*>/gi, '');
  await page.route('http://orchestrator.test/**', route => route.fulfill({contentType: 'text/html', body: html}));
  await page.goto('http://orchestrator.test/');
  await page.addStyleTag({content: read('styles.css') + read('live-team.css')});
  await page.evaluate(({team}) => {
    window.posts = []; window.links = {}; window.teamState = null;
    window.confirm = () => true;
    window.chatComposerAccessPreference = () => 'ask';
    window.request = async (url, options) => {
      if (options?.method === 'POST') {
        const body = JSON.parse(options.body); window.posts.push(body);
        if (body.action === 'chat_mode') { window.links[body.chat] = {...(window.links[body.chat] || {team_id: ''}), mode: body.mode}; return {chat: {mode: body.mode, team_id: window.links[body.chat].team_id, team: null}}; }
        if (body.action === 'create') { window.links[body.chat] = {mode: 'live_team', team_id: 'team-1'}; window.teamState = 'running'; return {team: {...team, state: 'starting'}}; }
        if (body.action === 'reopen') { window.teamState = 'running'; return {team: {...team, state: 'starting'}}; }
        return {};
      }
      if (url === '/api/live-team') return {agents: [], saved: [], running: [], chats: Object.fromEntries(Object.entries(window.links).map(([k, v]) => [k, {...v, state: window.teamState || ''}]))};
      if (url.startsWith('/api/live-team/chat')) {
        const chat = new URL(url, 'http://x').searchParams.get('chat'); const link = window.links[chat] || {mode: 'nexus', team_id: ''};
        return {schema_version: 1, chat, mode: link.mode, team_id: link.team_id, team: link.team_id ? (window.teamState === 'saved' ? {team_id: link.team_id, name: team.name, state: 'saved', agents: team.agents.map(a => ({id: a.id, name: a.name}))} : {...team, state: window.teamState}) : null};
      }
      if (url.startsWith('/api/live-team/team')) {
        if (window.teamState === 'saved') throw new Error('That team is not running. Reopen it first.');
        const after = Number(new URL(url, 'http://x').searchParams.get('after'));
        return {team: {...team, state: window.teamState}, events: [
          {seq: 1, kind: 'message', agent: 'builder', role: 'user', text: 'Build a game'},
          {seq: 2, kind: 'message', agent: 'builder', delta: false, text: 'On it: splitting the work.'},
          {seq: 3, kind: 'notice', level: 'delivery', agent: 'builder', to: 'reviewer', text: 'Review the level please'}].filter(e => e.seq > after)};
      }
      throw new Error('unexpected ' + url);
    };
    for (let one = document.getElementById('theBigChat'); one; one = one.parentElement) one.hidden = false;
    document.getElementById('theBigChatConversationList').innerHTML =
      '<button class="the-big-chat-conversation-pick" data-chat-id="pair-chat-1"><strong>Chat 1</strong></button>' +
      '<button class="the-big-chat-conversation-pick" data-chat-id="pair-chat-2"><strong>Chat 2</strong></button>';
  }, {team});
  await page.addScriptTag({content: read('live-team.js')});
  await page.addScriptTag({content: read('chat-orchestrator.js')});
  return page;
}

test('each board chat chooses Nexus or a Live team, and live teams stream per chat', {skip: !executablePath, timeout: 60000}, async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  const output = fs.mkdtempSync(path.join(os.tmpdir(), 'nexus-chat-orchestrator-'));
  try {
    const page = await open(browser);
    const sync = chat => page.evaluate(chat => window.nexusChatOrchestrator.sync('builder', chat), chat);
    await sync(pairChat);
    const nexus = page.locator('#theBigChatUseNexus'), live = page.locator('#theBigChatUseLiveTeam');
    await page.waitForFunction(() => document.getElementById('theBigChatUseNexus')?.getAttribute('aria-pressed') === 'true');
    // Default: the Nexus conversation, unchanged.
    assert.equal(await page.getByRole('group', {name: 'Orchestrator for this chat'}).isVisible(), true);
    assert.equal(await page.locator('#theBigChatChatPanel').isVisible(), true);
    assert.equal(await page.locator('#theBigChatLiveTeam').isVisible(), false);
    // Switching this chat to a live team is saved on the server and swaps the view.
    await live.click();
    await page.waitForFunction(() => document.getElementById('theBigChatUseLiveTeam').getAttribute('aria-pressed') === 'true');
    assert.deepEqual(await page.evaluate(() => window.posts[0]), {action: 'chat_mode', chat: 'pair-chat-1', mode: 'live_team'});
    assert.equal(await page.locator('#theBigChatChatPanel').isVisible(), false);
    assert.equal(await page.getByRole('tab', {name: 'CHAT', exact: true}).isVisible(), false);
    assert.equal(await page.locator('#theBigChatLiveTeam').isVisible(), true);
    assert.match(await page.locator('#theBigChatLiveTeam .lt-empty').innerText(), /No live team yet/);
    assert.match(await page.locator('.chat-live-team-about').innerText(), /Builder ↔ Reviewer · works in D:\/portable\/game · asks before commands/);
    // The first message starts this chat's own team with its agents, project and access.
    const box = page.locator('#theBigChatLiveTeam textarea');
    await box.fill('Build a game');
    await box.press('Enter');
    await page.waitForFunction(() => window.posts.some(p => p.action === 'create'));
    const created = await page.evaluate(() => window.posts.find(p => p.action === 'create'));
    assert.deepEqual({...created, name: undefined}, {action: 'create', chat: 'pair-chat-1', agents: ['builder', 'reviewer'], lead: 'builder',
      goal: 'Build a game', project: 'D:/portable/game', access: 'ask', mode: 'shared', name: undefined});
    await page.locator('#theBigChatLiveTeam .lt-delivery').waitFor();
    assert.deepEqual(await page.locator('#theBigChatLiveTeam .lt-message p').allInnerTexts(), ['On it: splitting the work.']);
    assert.match(await page.locator('#theBigChatLiveTeam .lt-chips').innerText(), /Builder \(lead\)/);
    assert.match(await page.locator('#theBigChatLiveTeam .lt-tasks').innerText(), /Build the level/);
    await page.waitForFunction(() => /Live team · running/.test(document.querySelector('[data-chat-id="pair-chat-1"]').innerText));
    // Messages to a running team go to it, not to a new team.
    await box.fill('Add a boss fight');
    await box.press('Enter');
    await page.waitForFunction(() => window.posts.some(p => p.action === 'say'));
    assert.deepEqual(await page.evaluate(() => window.posts.find(p => p.action === 'say')), {action: 'say', team: 'team-1', text: 'Add a boss fight', to: ''});
    for (const width of [1264, 760, 390]) {
      await page.setViewportSize({width, height: 850});
      const layout = await page.evaluate(() => {
        const composer = document.querySelector('#theBigChatLiveTeam .lt-composer textarea').getBoundingClientRect();
        return {overflow: document.documentElement.scrollWidth > innerWidth + 1, composerVisible: composer.width > 60 && composer.bottom <= innerHeight + 1 && composer.top >= 0};
      });
      assert.deepEqual(layout, {overflow: false, composerVisible: true}, `width ${width}`);
      await page.screenshot({path: path.join(output, `${width}-live.png`)});
    }
    await page.setViewportSize({width: 1264, height: 850});
    // Another chat keeps its own orchestrator; the first team is kept, not lost.
    await sync(otherChat);
    await page.waitForFunction(() => document.getElementById('theBigChatUseNexus').getAttribute('aria-pressed') === 'true');
    assert.equal(await page.locator('#theBigChatChatPanel').isVisible(), true);
    assert.equal(await page.locator('#theBigChatLiveTeam').isVisible(), false);
    await sync(pairChat);
    await page.waitForFunction(() => document.getElementById('theBigChatUseLiveTeam').getAttribute('aria-pressed') === 'true');
    assert.deepEqual(await page.locator('#theBigChatLiveTeam .lt-message p').allInnerTexts(), ['On it: splitting the work.']);
    // A saved team that is not running is reopened by the next message, which it then receives.
    await page.evaluate(() => { window.teamState = 'saved'; });
    await page.waitForFunction(() => /not running/.test(document.querySelector('#theBigChatLiveTeam .lt-status')?.innerText || ''), null, {timeout: 10000});
    await box.fill('Carry on');
    await box.press('Enter');
    await page.waitForFunction(() => window.posts.some(p => p.action === 'reopen'));
    assert.deepEqual(await page.evaluate(() => window.posts.find(p => p.action === 'reopen')), {action: 'reopen', team: 'team-1', text: 'Carry on', to: ''});
    // Back to Nexus: the saved conversation returns for this chat.
    await nexus.click();
    await page.waitForFunction(() => document.getElementById('theBigChatUseNexus').getAttribute('aria-pressed') === 'true');
    assert.equal(await page.locator('#theBigChatChatPanel').isVisible(), true);
    assert.equal(await page.getByRole('tab', {name: 'CHAT', exact: true}).isVisible(), true);
    console.log('Chat orchestrator screenshots:', output);
  } finally { await browser.close(); }
});
