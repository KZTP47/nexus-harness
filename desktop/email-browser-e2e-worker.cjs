'use strict';
// Test-only subprocess transport. The production worker is unmodified: route
// all browser requests into the synthetic DOM before any mailbox navigation.
const fs = require('node:fs');
const readline = require('node:readline');
const {chromium} = require('playwright-core');
const {createFixture} = require('./email-browser-fixture.cjs');
const statePath = process.env.NEXUS_BROWSER_E2E_STATE;
const receiptPath = process.env.NEXUS_BROWSER_E2E_RECEIPT;
if (!statePath || !receiptPath) throw new Error('Synthetic browser fixture paths are required.');
const fixture = createFixture({state: JSON.parse(fs.readFileSync(statePath, 'utf8')), receiptPath});
let serializedState = fs.readFileSync(statePath, 'utf8');
const contexts = new Set();
if (fs.existsSync(receiptPath)) fixture.state.sendCount = JSON.parse(fs.readFileSync(receiptPath, 'utf8')).sendCount;
const originalLaunch = chromium.launchPersistentContext.bind(chromium);
chromium.launchPersistentContext = async (...args) => {
  const context = await originalLaunch(...args);
  await context.route('**/*', fixture.route);
  contexts.add(context);
  context.on('close', () => contexts.delete(context));
  return context;
};
const worker = require('./email-browser-worker.js');
const lines = readline.createInterface({input: process.stdin});
let chain = Promise.resolve();
lines.on('line', line => {
  chain = chain.then(async () => {
    try {
      const latest = fs.readFileSync(statePath, 'utf8');
      if (latest !== serializedState) {
        serializedState = latest;
        Object.assign(fixture.state, JSON.parse(latest));
        // Simulate provider-pushed inbox DOM changes. Gmail hash navigation
        // alone does not reload a static document as a real inbox would update.
        for (const context of contexts) for (const page of context.pages()) {
          if (page.url() !== 'about:blank') await page.reload({waitUntil: 'domcontentloaded'});
        }
      }
      const result = await worker.handle(JSON.parse(line));
      process.stdout.write(JSON.stringify({result}) + '\n');
    } catch (error) {
      process.stdout.write(JSON.stringify({error: String(error.message || error)}) + '\n');
    }
  });
});
lines.on('close', () => worker.close().finally(() => process.exit(0)));
process.on('SIGTERM', () => worker.close().finally(() => process.exit(0)));
