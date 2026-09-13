'use strict';
// Real Electron + real local HTTP server. Input must be an isolated synthetic
// project whose automatic records were produced by backend revise_draft.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {_electron} = require('playwright-core');
async function main() {
  if (!process.argv[2]) throw new Error('Pass an isolated synthetic fixture JSON');
  const fixture = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  for (const key of ['root','project','user_data','account_id']) assert.ok(fixture[key], 'Fixture requires ' + key);
  const source = process.env.NEXUS_EMAIL_SOURCE_SMOKE === '1';
  const app = await _electron.launch({executablePath:source ? require('electron') : path.join(__dirname,'build-output/win-unpacked/Nexus Harness.exe'), args:[...(source ? [__dirname] : []), '--project', fixture.project, '--user-data-dir=' + fixture.user_data], timeout:60000});
  try {
    assert.equal(path.resolve(await app.evaluate(({app}) => app.getPath('userData'))).toLowerCase(), path.resolve(fixture.user_data).toLowerCase());
    const page = await app.firstWindow(); await page.waitForURL(/^http:\/\/127\.0\.0\.1:/, {timeout:60000});
    await page.locator('[data-view="email"]').click();
    await page.locator('#emailAccount').selectOption(fixture.account_id);
    await page.evaluate(() => window.nexusEmail.refresh());
    const before = await page.evaluate(() => window.request('/api/email'));
    const automatic = before.automatic_memories.filter(m => m.account_id === fixture.account_id && m.status === 'active');
    assert.ok(automatic.length >= 2); assert.ok(before.memories.some(m => m.account_id === fixture.account_id));
    await page.locator('#emailAutomaticLearningTab').click();
    assert.equal(await page.locator('#emailAutomaticLearningTab').textContent(), 'What your assistant has learned AUTOMATICALLY');
    const outcomes = (before.automatic_learning_outcomes || []).filter(item => item.account_id === fixture.account_id);
    const recipients = [...new Set([...automatic.map(m => m.recipient), ...outcomes.map(item => item.recipient || 'Recipient unavailable')])];
    assert.equal(await page.locator('.email-recipient-memory').count(), recipients.length);
    for (const recipient of recipients) assert.ok((await page.locator('.email-recipient-memory h3').allTextContents()).includes(recipient));
    for (const outcome of outcomes.filter(item => ['no_reusable_preferences', 'failed'].includes(item.status))) {
      const group = page.locator('.email-recipient-memory').filter({has: page.getByRole('heading', {name: outcome.recipient, exact: true})});
      assert.match(await group.innerText(), outcome.status === 'failed' ? /Automatic learning failed/ : /found no reusable preference/);
      assert.ok(await group.getByRole('button', {name: 'Retry learning', exact: true}).count());
    }
    if (outcomes.length) assert.equal(await page.locator('.email-memory-empty').count(), 0, 'Recorded attempts must not tell the user to revise for the first time');
    await page.locator('#emailAutomaticLearningTab').focus();
    await page.screenshot({path:path.join(fixture.root, 'email-automatic-desktop.png')});
    await page.setViewportSize({width:760,height:850});
    await page.locator('.email-memory').scrollIntoViewIfNeeded();
    await page.locator('#emailAutomaticLearningTab').focus();
    const dimensions = await page.evaluate(() => ({width:document.documentElement.clientWidth,content:document.documentElement.scrollWidth}));
    assert.ok(dimensions.content <= dimensions.width + 2, 'Narrow window has no horizontal overflow');
    await page.screenshot({path:path.join(fixture.root,'email-automatic-narrow.png')});
    const noRule = outcomes.find(item => item.status === 'no_reusable_preferences');
    if (noRule) await page.locator('.email-recipient-memory').filter({has:page.getByRole('heading',{name:noRule.recipient,exact:true})}).screenshot({path:path.join(fixture.root,'email-automatic-no-rule.png')});
    const target = automatic[0]; const edited = target.text + ' Keep the greeting brief.';
    const input = page.locator('#email-memory-automatic-' + target.id); await input.fill(edited);
    await page.evaluate(() => window.nexusEmail.refresh()); assert.equal(await input.inputValue(), edited);
    await input.locator('..').getByRole('button',{name:'Save',exact:true}).click();
    await page.waitForFunction(() => document.querySelector('#emailNotice').textContent === 'Saved.');
    await page.reload(); await page.locator('[data-view="email"]').click(); await page.locator('#emailAccount').selectOption(fixture.account_id); await page.locator('#emailAutomaticLearningTab').click();
    assert.equal(await input.inputValue(), edited);
    await input.locator('..').getByRole('button',{name:'Delete',exact:true}).click();
    await page.waitForFunction(id => !document.getElementById('email-memory-automatic-' + id), target.id);
    await page.reload(); await page.locator('[data-view="email"]').click(); await page.locator('#emailAccount').selectOption(fixture.account_id); await page.locator('#emailAutomaticLearningTab').click();
    assert.equal(await input.count(), 0);
    const after = await page.evaluate(() => window.request('/api/email'));
    assert.deepEqual(after.memories, before.memories, 'Automatic edits preserve manual memory');
    for (const m of automatic.filter(m => m.id !== target.id)) assert.equal(after.automatic_memories.find(item => item.id === m.id).text, m.text, 'Other recipients remain unchanged');
    console.log(JSON.stringify({automatic_learning_electron:'passed',source,recipient_groups:recipients.length,learning_outcomes:outcomes.length,save_delete_survive_reload:true,manual_preserved:true,screenshots:['email-automatic-desktop.png','email-automatic-narrow.png'],real_mail_sent:false}));
  } finally { await app.close(); }
}
main().catch(error => {console.error(error);process.exitCode=1;});
