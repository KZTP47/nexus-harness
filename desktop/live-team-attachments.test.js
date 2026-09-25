// Live team view: agent Markdown is formatted (never run as HTML), files can be
// attached by button, paste or drop, and browser checks switch between hidden
// and visible. Runs the real live-team.js in the bundled Chromium with a fake API.
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

const team = {team_id: 't1', name: 'Game', goal: 'g', project: 'C:/work', lead: 'lead', access: 'full', mode: 'shared',
  state: 'running', browser: 'hidden', seq: 0, tasks: [], transitions: [], messages: [], leases: [], questions: [], results: [],
  agents: [{id: 'lead', name: 'Lead', session: {state: 'ready'}}, {id: 'helper', name: 'Helper', session: {state: 'ready'}}]};
const report = [
  '## How to Play',
  'Open **index.html** and press `WASD`. See [the docs](https://example.test/docs).',
  '',
  '- **Move** with WASD',
  '- *Build* with G',
  '1. First',
  '2. Second',
  '```',
  '<script>window.hacked = true</script>',
  '```',
  '<img src=x onerror="window.hacked = true">',
].join('\n');
const events = [
  {seq: 1, kind: 'message', agent: 'lead', delta: false, text: report, id: 'm1'},
  {seq: 2, kind: 'notice', level: 'result', agent: 'helper', status: 'done', text: 'Checked with **check_page**: no errors.'},
];

async function page(browser) {
  const one = await browser.newPage();
  await one.setContent('<main id="liveTeamView"></main>');
  await one.evaluate(({team, events}) => {
    window.posts = []; window.hacked = false;
    window.request = async (url, options) => {
      if (options?.method === 'POST') { window.posts.push(JSON.parse(options.body)); return {settings: {browser: 'visible'}}; }
      if (url === '/api/live-team') return {project: 'C:/work', agents: [], saved: [], running: [team], settings: {browser: 'hidden'}};
      if (url.startsWith('/api/live-team/team')) {
        const after = Number(new URL(url, 'http://x').searchParams.get('after'));
        return {team, events: events.filter(one => one.seq > after)};
      }
      throw new Error('unexpected ' + url);
    };
  }, {team, events});
  await one.addScriptTag({content: source});
  await one.evaluate(() => window.nexusLiveTeam.refresh());
  await one.waitForSelector('.lt-result');
  return one;
}

test('agent Markdown is formatted and nothing in it runs', {skip: !executablePath, timeout: 45000}, async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const view = await page(browser);
    const message = '.lt-message .lt-md';
    assert.equal(await view.textContent(`${message} .lt-md-h`), 'How to Play');
    assert.deepEqual(await view.$$eval(`${message} strong`, nodes => nodes.map(n => n.textContent)), ['index.html', 'Move']);
    assert.equal(await view.textContent(`${message} p code`), 'WASD');
    assert.equal(await view.textContent(`${message} em`), 'Build');
    assert.deepEqual(await view.$$eval(`${message} ul li`, nodes => nodes.map(n => n.textContent)), ['Move with WASD', 'Build with G']);
    assert.deepEqual(await view.$$eval(`${message} ol li`, nodes => nodes.map(n => n.textContent)), ['First', 'Second']);
    assert.equal(await view.getAttribute(`${message} a`, 'href'), 'https://example.test/docs');
    assert.equal(await view.getAttribute(`${message} a`, 'rel'), 'noopener noreferrer');
    assert.equal(await view.textContent(`${message} pre code`), '<script>window.hacked = true</script>');
    // No raw Markdown markers left, and no markup from the agent became elements.
    assert.doesNotMatch(await view.textContent(message), /\*\*|##/);
    assert.equal(await view.$$eval('.lt-timeline script, .lt-timeline img', nodes => nodes.length), 0);
    await view.waitForTimeout(100);
    assert.equal(await view.evaluate(() => window.hacked), false);
    assert.equal(await view.textContent('.lt-result .lt-md strong'), 'check_page');
  } finally {
    await browser.close();
  }
});

test('files attach by button, paste and drop, and go with the message', {skip: !executablePath, timeout: 45000}, async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const view = await page(browser);
    assert.equal(await view.getAttribute('#ltAttach', 'aria-label'), 'Attach files');
    await view.setInputFiles('#ltFiles', {name: 'reference.png', mimeType: 'image/png', buffer: Buffer.from('png-bytes')});
    await view.waitForSelector('.lt-attachment');
    assert.match(await view.textContent('.lt-attachments'), /reference\.png · 1 KB/);
    // A pasted screenshot joins the same message.
    await view.evaluate(() => {
      const data = new DataTransfer();
      data.items.add(new File(['shot'], 'pasted.png', {type: 'image/png'}));
      document.getElementById('ltText').dispatchEvent(new ClipboardEvent('paste', {clipboardData: data, bubbles: true, cancelable: true}));
    });
    await view.waitForFunction(() => document.querySelectorAll('.lt-attachment').length === 2);
    // Ordinary text paste is left to the browser.
    const plain = await view.evaluate(() => {
      const data = new DataTransfer(); data.setData('text/plain', 'words');
      const event = new ClipboardEvent('paste', {clipboardData: data, bubbles: true, cancelable: true});
      document.getElementById('ltText').dispatchEvent(event);
      return event.defaultPrevented;
    });
    assert.equal(plain, false);
    // A dropped file too; then one is removed again.
    await view.evaluate(() => {
      const data = new DataTransfer();
      data.items.add(new File(['drop'], 'dropped.txt', {type: 'text/plain'}));
      document.querySelector('.lt-composer').dispatchEvent(new DragEvent('drop', {dataTransfer: data, bubbles: true, cancelable: true}));
    });
    await view.waitForFunction(() => document.querySelectorAll('.lt-attachment').length === 3);
    await view.click('.lt-attachment-remove[aria-label="Remove dropped.txt"]');
    await view.waitForFunction(() => document.querySelectorAll('.lt-attachment').length === 2);
    // Files alone can be sent; the chips clear once they are on their way.
    await view.click('#ltSend');
    await view.waitForFunction(() => window.posts.some(p => p.action === 'say'));
    const said = await view.evaluate(() => window.posts.find(p => p.action === 'say'));
    assert.equal(said.text, '');
    assert.deepEqual(said.attachments.map(one => [one.name, one.type]), [['reference.png', 'image/png'], ['pasted.png', 'image/png']]);
    assert.equal(Buffer.from(said.attachments[0].data.split(',')[1], 'base64').toString(), 'png-bytes');
    assert.equal(await view.$$eval('.lt-attachment', nodes => nodes.length), 0);
    // Too many files are refused with a reason, not silently dropped.
    await view.setInputFiles('#ltFiles', Array.from({length: 11}, (_, i) => ({name: `f${i}.txt`, mimeType: 'text/plain', buffer: Buffer.from('x')})));
    await view.waitForFunction(() => /at most 10/.test(document.querySelector('.lt-main .lt-note').textContent));
  } finally {
    await browser.close();
  }
});

test('browser checks switch between hidden and visible for the running team', {skip: !executablePath, timeout: 45000}, async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const view = await page(browser);
    assert.equal(await view.inputValue('#ltBrowser'), 'hidden');
    assert.match(await view.getAttribute('#ltBrowser', 'title') || '', /no tabs or windows open on your screen/);
    await view.selectOption('#ltBrowser', 'visible');
    await view.waitForFunction(() => window.posts.some(p => p.action === 'browser'));
    assert.deepEqual(await view.evaluate(() => window.posts.find(p => p.action === 'browser')), {action: 'browser', mode: 'visible', team: 't1'});
    assert.match(await view.textContent('.lt-main .lt-note'), /you will see the browser/);
    // The start form offers the same choice for new teams.
    assert.equal(await view.inputValue('#ltStartBrowser'), 'hidden');
    assert.equal(await view.$$eval('[id="ltBrowser"]', nodes => nodes.length), 1);
  } finally {
    await browser.close();
  }
});
