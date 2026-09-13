"use strict";

// Explicit synthetic fixture; starts only the fixture's private mailbox service.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {_electron} = require('playwright-core');

async function main() {
  if (!process.argv[2]) throw new Error('Pass the isolated managed-service fixture JSON');
  const fixture = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  const app = await _electron.launch({
    executablePath: path.join(__dirname, 'build-output', 'win-unpacked', 'Nexus Harness.exe'),
    args: ['--project', fixture.project, '--user-data-dir=' + fixture.user_data], timeout: 60000,
  });
  try {
    assert.equal(path.resolve(await app.evaluate(({app}) => app.getPath('userData'))), path.resolve(fixture.user_data));
    const page = await app.firstWindow();
    await page.getByRole('button', {name: 'Email assistant', exact: true}).click();
    await page.locator('#emailConnectionMethod').selectOption('emailengine');
    await page.getByRole('button', {name: 'Set up local mailbox service', exact: true}).click();
    await page.waitForFunction(() => /Local mailbox service is ready/.test(document.querySelector('#emailManagedStatus').textContent), null, {timeout: 90000});
    await page.getByRole('button', {name: 'Find service mailboxes', exact: true}).click();
    await page.waitForFunction(() => /Mailbox list refreshed/.test(document.querySelector('#emailNotice').textContent));
    await page.getByRole('button', {name: 'Add mailbox / sign in', exact: true}).click();
    await page.locator('#emailManagedSignInLink').waitFor({state: 'visible'});
    const state = await page.evaluate(() => request('/api/email'));
    assert.equal(state.accounts.length, 0, 'Fixture has no real mailbox');
    assert.equal(state.emailengine.local_runtime.ready, true);
    assert.equal(state.emailengine.token, undefined, 'API token stays out of public state');
    const form = new URL(await page.locator('#emailManagedSignInLink').getAttribute('href'));
    assert.equal(form.origin, new URL(state.emailengine.url).origin);
    await page.locator('#emailManagedService').screenshot({path: path.join(fixture.root, 'packaged-mailbox-service.png')});
    await page.setViewportSize({width: 760, height: 850});
    const dimensions = await page.evaluate(() => ({width: document.documentElement.clientWidth, content: document.documentElement.scrollWidth}));
    assert.ok(dimensions.content <= dimensions.width + 2);
    console.log(JSON.stringify({packaged_managed_mail: 'passed', private_service_restart: true, hosted_sign_in: true, real_mail_sent: false}));
  } finally {
    await app.close();
  }
}
main().catch(error => {console.error(error); process.exitCode = 1;});
