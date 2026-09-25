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
const source = fs.readFileSync(path.join(ui, 'live-team.js'), 'utf8');

const team = {team_id: 't1', name: 'Unicorn', goal: 'g', project: 'C:/work', lead: 'codex', access: 'full', mode: 'shared', state: 'running', seq: 0,
  agents: [{id: 'codex', name: 'GPT Codex', seat: 'codex@t1', session: {state: 'ready', busy: true, session_id: 'thread-1', resume: 'resumed'}},
           {id: 'claude', name: 'Claude', seat: 'claude@t1', session: {state: 'ready', busy: false, session_id: 's-2'}}],
  tasks: [{id: 'task-1', title: 'Build it', owner: 'codex', state: 'done', closure_reason: 'handed_off', closure_target: 'claude'}],
  transitions: [], messages: [{sender: 'codex', recipient: 'claude', text: 'hi'}], leases: [{pattern: 'src/*', holder: 'codex'}], questions: [], results: []};

const events = [
  {seq: 1, kind: 'message', agent: 'codex', role: 'briefing', text: 'Team: GPT Codex, Claude. You lead.'},
  {seq: 2, kind: 'message', agent: 'codex', delta: true, text: 'Buil', id: 'm1'},
  {seq: 3, kind: 'message', agent: 'codex', delta: true, text: 'ding <b>now</b>', id: 'm1'},
  {seq: 4, kind: 'tool', agent: 'codex', id: 'c1', status: 'running', title: 'npm test'},
  {seq: 5, kind: 'tool', agent: 'codex', id: 'c1', status: 'failed', title: 'npm test', output: '2 failed'},
  {seq: 6, kind: 'thought', agent: 'claude', delta: true, text: ''},
  {seq: 7, kind: 'thought', agent: 'claude', delta: false, text: 'Check the API'},
  {seq: 8, kind: 'notice', level: 'delivery', agent: 'codex', to: 'claude', text: 'Please review'},
  {seq: 9, kind: 'permission', agent: 'codex', question: 'q1', text: JSON.stringify({method: 'item/commandExecution/requestApproval', command: '"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command \'rm -rf build\'', cwd: 'C:/work', reason: 'cleanup'})},
  {seq: 10, kind: 'notice', level: 'question', agent: 'claude', question: 'q2', text: 'Blue or red?'},
  {seq: 11, kind: 'notice', level: 'result', agent: 'codex', status: 'done', text: 'All built'},
  {seq: 12, kind: 'message', agent: 'codex', delta: false, text: 'Building now, done.', id: 'm1'},
  {seq: 13, kind: 'turn', agent: 'claude', status: 'interrupted'},
];

async function page(browser) {
  const one = await browser.newPage();
  await one.setContent('<main id="liveTeamView"></main>');
  await one.evaluate(({team, events}) => {
    window.posts = []; window.served = false;
    window.confirm = () => true;
    window.request = async (url, options) => {
      if (options?.method === 'POST') {
        const body = JSON.parse(options.body); window.posts.push(body);
        if (body.action === 'create') return {team: {team_id: 't1'}};
        return {};
      }
      if (url === '/api/live-team') return {project: 'C:/work', agents: [
        {id: 'codex', name: 'GPT Codex', kind: 'codex-cli', model: 'gpt', supported: true},
        {id: 'claude', name: 'Claude', kind: 'claude-cli', model: '', supported: true},
        {id: 'web', name: 'Web helper', kind: '', route: 'web:chatgpt', supported: false}], saved: [], running: [team]};
      if (url.startsWith('/api/live-team/team')) {
        const after = Number(new URL(url, 'http://x').searchParams.get('after'));
        return {team, events: events.filter(one => one.seq > after)};
      }
      throw new Error('unexpected ' + url);
    };
  }, {team, events});
  await one.addScriptTag({content: source});
  await one.evaluate(() => window.nexusLiveTeam.refresh());
  return one;
}

test('the live team page streams, updates, and answers through the API', {skip: !executablePath, timeout: 45000}, async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const view = await page(browser);
    await view.waitForSelector('.lt-result');
    // The start form: accessible names, unsupported agents explained.
    assert.equal(await view.getAttribute('.lt-agent-choice input[value="codex"]', 'aria-label'), 'GPT Codex');
    assert.equal(await view.isDisabled('.lt-agent-choice input[value="web"]'), true);
    assert.match(await view.textContent('.lt-agents'), /web:chatgpt · not supported yet/);
    assert.equal(await view.inputValue('#ltProject'), 'C:/work');
    // Streaming: deltas grow one bubble; the final text replaces it; HTML stays text.
    const messages = await view.$$eval('.lt-message p', nodes => nodes.map(n => n.textContent));
    assert.deepEqual(messages, ['Building now, done.']);
    assert.equal(await view.$$eval('.lt-message b', nodes => nodes.length), 0);
    // A tool row updates in place.
    assert.equal(await view.$$eval('.lt-tool', nodes => nodes.length), 1);
    assert.equal(await view.getAttribute('.lt-tool', 'data-status'), 'failed');
    assert.match(await view.textContent('.lt-tool'), /✗\s*npm test/);
    // An empty thinking delta makes no row; the real thought does.
    assert.deepEqual(await view.$$eval('.lt-thought p', nodes => nodes.map(n => n.textContent)), ['Check the API']);
    assert.match(await view.textContent('.lt-briefing summary'), /Nexus briefed GPT Codex/);
    assert.match(await view.textContent('.lt-delivery'), /→ Claude/);
    // A stopped turn says so, and the composer has an accessible name.
    assert.match(await view.textContent('.lt-timeline'), /Stopped\. Send a message to carry on\./);
    assert.equal(await view.getAttribute('#ltText', 'aria-label'), 'Message the team');
    // Status chips and the board.
    assert.match(await view.textContent('.lt-chips'), /GPT Codex \(lead\)working/);
    assert.match(await view.textContent('.lt-chips'), /conversation resumed/);
    assert.match(await view.textContent('.lt-tasks'), /handed off → Claude/);
    assert.match(await view.textContent('.lt-leases'), /src\/\* · GPT Codex/);
    // Approvals read as plain words, not JSON.
    assert.match(await view.textContent('.lt-ask'), /Wants to run a command in C:\/work:/);
    assert.equal(await view.textContent('.lt-ask pre'), 'rm -rf build');
    assert.match(await view.textContent('.lt-ask'), /Reason given: cleanup/);
    // Approvals and questions post exact answers.
    await view.click('.lt-ask button:has-text("Deny")');
    await view.fill('.lt-ask input', 'Blue');
    await view.click('.lt-ask button:has-text("Answer")');
    await view.waitForFunction(() => window.posts.length === 2);
    assert.deepEqual(await view.evaluate(() => window.posts), [
      {action: 'answer', team: 't1', question: 'q1', answer: 'decline'},
      {action: 'answer', team: 't1', question: 'q2', answer: 'Blue'}]);
    // Messaging one agent.
    await view.selectOption('#ltTo', 'claude');
    await view.fill('#ltText', 'Close your task please');
    await view.press('#ltText', 'Enter');
    await view.waitForFunction(() => window.posts.length === 3);
    assert.deepEqual(await view.evaluate(() => window.posts[2]), {action: 'say', team: 't1', text: 'Close your task please', to: 'claude'});
  } finally {
    await browser.close();
  }
});

test('starting a team posts the chosen agents, lead and settings', {skip: !executablePath, timeout: 45000}, async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const view = await page(browser);
    await view.waitForSelector('.lt-agent-choice');
    await view.click('#ltStart');
    assert.match(await view.textContent('.lt-note'), /Choose at least one agent/);
    await view.check('.lt-agent-choice input[value="codex"]');
    await view.check('.lt-agent-choice input[value="claude"]');
    await view.selectOption('#ltLead', 'claude');
    await view.selectOption('#ltAccess', 'ask');
    await view.selectOption('#ltMode', 'worktrees');
    await view.fill('#ltGoal', 'Make a game');
    await view.click('#ltStart');
    await view.waitForFunction(() => window.posts.some(p => p.action === 'create'));
    assert.deepEqual(await view.evaluate(() => window.posts.find(p => p.action === 'create')),
      {action: 'create', agents: ['codex', 'claude'], lead: 'claude', goal: 'Make a game', project: 'C:/work', access: 'ask', mode: 'worktrees', browser: 'hidden'});
  } finally {
    await browser.close();
  }
});

test('a reopened team (new run) replaces the timeline instead of hiding its events', {skip: !executablePath, timeout: 45000}, async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const view = await browser.newPage();
    await view.setContent('<main id="liveTeamView"></main>');
    await view.evaluate(({team}) => {
      window.polls = 0;
      window.request = async url => {
        if (url === '/api/live-team') return {project: 'C:/work', agents: [], saved: [], running: [team]};
        window.polls += 1;
        if (window.polls === 1) return {team: {...team, run: 'first'}, events: [{seq: 300, kind: 'message', agent: 'codex', text: 'Old run'}]};
        return {team: {...team, run: 'second'}, events: [{seq: 5, kind: 'message', agent: 'codex', text: 'New run'}].filter(one => one.seq > Number(new URL(url, 'http://x').searchParams.get('after')))};
      };
    }, {team});
    await view.addScriptTag({content: source});
    await view.evaluate(() => window.nexusLiveTeam.refresh());
    await view.waitForFunction(() => [...document.querySelectorAll('.lt-message p')].some(p => p.textContent === 'New run'), null, {timeout: 20000});
    assert.deepEqual(await view.$$eval('.lt-message p', nodes => nodes.map(n => n.textContent)), ['New run']);
  } finally {
    await browser.close();
  }
});
