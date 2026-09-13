"use strict";

// Reopen an explicitly supplied synthetic acceptance fixture in real Electron.
// No provider call or SMTP send occurs in this test.
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const { _electron } = require("playwright-core");

async function main() {
  const fixtureFile = process.argv[2];
  if (!fixtureFile) throw new Error("Pass the synthetic UI acceptance fixture JSON explicitly");
  const fixture = JSON.parse(fs.readFileSync(fixtureFile, "utf8"));
  const source = process.env.NEXUS_EMAIL_SOURCE_SMOKE === "1";
  const app = await _electron.launch({
    executablePath: source ? require("electron") : path.join(__dirname, "build-output", "win-unpacked", "Nexus Harness.exe"),
    args: [...(source ? [__dirname] : []), `--user-data-dir=${fixture.user_data}`], timeout: 60000,
  });
  try {
    const actualUserData = await app.evaluate(({app}) => app.getPath('userData'));
    assert.equal(path.resolve(actualUserData).toLowerCase(), path.resolve(fixture.user_data).toLowerCase(), 'Acceptance must use its explicit isolated app profile');
    const page = await app.firstWindow();
    await page.waitForURL(/^http:\/\/127\.0\.0\.1:/, { timeout: 60000 });
    await page.locator('[data-view="email"]').click();
    await page.locator("#emailAccount").waitFor();
    await page.locator('#emailConnectionMethod').waitFor();
    assert.equal(await page.locator('#emailConnectionMethod').inputValue(), 'browser');
    assert.ok(await page.locator('#emailLocalOpen-browser_outlook').isVisible());
    assert.ok(await page.locator('#emailLocalOpen-browser_gmail').isVisible());
    for (const kind of ['browser_outlook', 'browser_gmail']) {
      const mode = page.locator('#emailBrowserMode-' + kind);
      assert.ok(await mode.isVisible());
      assert.match(await mode.innerText(), /Visible browser[\s\S]*Background browser \(headless\)/);
      await mode.selectOption('headless');
      assert.equal(await mode.inputValue(), 'headless');
      assert.match(await page.locator('#emailBrowserModeStatus-' + kind).innerText(), /visible browser/);
    }
    if (fixture.browser_connection_id) {
      // This is synthetic saved metadata only, with no authenticated mailbox.
      // Saving mode exercises the real renderer/HTTP/bridge/persistence path
      // and must not launch a browser or connect a mailbox.
      await page.locator('#emailBrowserModeSave-browser_outlook').click();
      await page.waitForFunction(() => /mode saved/i.test(document.querySelector('#emailNotice').textContent));
      await page.reload();
      await page.locator('[data-view="email"]').click();
      await page.locator('#emailBrowserMode-browser_outlook').waitFor();
      await page.waitForFunction(() => !document.querySelector('#emailBrowserMode-browser_outlook').disabled);
      assert.equal(await page.locator('#emailBrowserMode-browser_outlook').inputValue(), 'headless');
      const saved = await page.evaluate(() => request('/api/email'));
      assert.equal(saved.local_connections.find(c => c.id === fixture.browser_connection_id).browser_mode, 'headless');
    }
    await page.locator('#emailConnectionMethod').selectOption('classic');
    assert.ok(await page.locator('#emailLocalOpen-classic_outlook').isVisible());
    await page.locator('#emailConnectionMethod').selectOption('api');
    await page.locator('#emailAutoConnect-outlook').waitFor();
    await page.locator('#emailAutoConnect-gmail').waitFor();
    await page.waitForFunction(() => document.querySelector('#emailProvider').value && !document.querySelector('#emailAutoConnect-outlook').disabled);
    assert.match(await page.locator('#emailOAuthStatus-outlook').textContent(), /registration/i);
    assert.match(await page.locator('#emailOAuthStatus-gmail').textContent(), /registration/i);
    await page.locator('#emailAutoConnect-outlook').click();
    await page.waitForFunction(() => /client ID/.test(document.querySelector('#emailNotice').textContent), null, {timeout: 10000})
      .catch(async error => { throw new Error('Automatic connection notice: ' + await page.locator('#emailNotice').textContent() + '; ' + error.message); });
    assert.equal(await page.locator('#emailClientId-outlook').inputValue(), '');
    await page.locator('#emailProvider').selectOption('codex');
    assert.ok(await page.locator('#emailModel option[value="gpt-6-astra"]').count());
    assert.ok(await page.locator('#emailModel option[value="gpt-5.6-sol"]').count());
    await page.locator('#emailModel').selectOption('gpt-6-astra');
    await page.waitForFunction(() => document.querySelectorAll("#emailQueue button").length > 0);
    const data = await page.evaluate(() => request("/api/email"));
    assert.ok(data.accounts.every(a => a.kind === "import"), "Synthetic test never uses a live mailbox");
    const exported = data.drafts.find(d => d.status === "exported");
    assert.ok(exported, "The computer-use approved export survives restart");
    assert.ok(data.memories.some(m => m.source_draft_id === exported.id || m.evidence?.source_draft_id === exported.id));
    const message = data.messages.find(m => m.id === exported.message_id);
    await page.locator("#emailQueue button").filter({ hasText: message.subject }).first().click();
    assert.equal(await page.locator("#emailReply").inputValue(), exported.edited);
    const output = path.join(fixture.root, "verified-download.eml");
    // Exercise real renderer→preload→main IPC and filesystem effects, replacing
    // only the operating system file-picker choice with a synthetic test path.
    await app.evaluate(({dialog}, selected) => {
      dialog.showSaveDialogSync = (_window, options) => {
        if (options.title !== "Save approved email reply") throw new Error("Wrong save dialog");
        return selected;
      };
    }, output);
    await page.locator("#emailDownload").click();
    await page.waitForFunction(() => document.querySelector("#emailNotice").textContent.includes(".eml"));
    const content = fs.readFileSync(output, "utf8");
    assert.match(content, /Warm wishes/);
    assert.match(content, /To: colleague@example\.test/);
    // Inject snapshots only into this isolated renderer. No draft mutation or
    // provider/send action is posted to the running application for these checks.
    const activity = structuredClone(data);
    const activeDraft = activity.drafts.find(d => d.id === exported.id);
    activity.accounts.find(account => account.id === exported.account_id).kind = 'browser_outlook';
    activeDraft.export_path = '';
    const started = new Date(Date.now() - 12000).toISOString();
    const activityRoute = async route => {
      assert.equal(route.request().method(), 'GET', 'Activity probe must never mutate a draft');
      activity.captured_at = new Date().toISOString();
      await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(activity)});
    };
    await page.route('**/api/email', activityRoute);
    try {
      Object.assign(activeDraft, {status: 'generating', error: ''});
      activity.operations = [{id: 'draft:' + activeDraft.id, state: 'completed'}, {id: 'generate:' + activeDraft.id, state: 'running', started_at: started}];
      await page.evaluate(() => window.nexusEmail.refresh());
      assert.equal(await page.locator('#emailGenerateStatus').getAttribute('data-phase'), 'running');
      assert.match(await page.locator('#emailGenerateStatus').innerText(), /Last reported[\s\S]*\ds elapsed/);
      assert.ok(await page.locator('#emailGenerateStatus .is-running').isVisible());
      await page.locator('.email-review').screenshot({path: path.join(fixture.root, 'email-activity-generation.png')});
      activeDraft.status = 'review';
      activity.operations = [{id: 'generate:' + activeDraft.id, state: 'completed'}, {id: 'revise:' + activeDraft.id, state: 'running', started_at: started}];
      await page.evaluate(() => window.nexusEmail.refresh());
      assert.equal(await page.locator('#emailRevisionStatus').getAttribute('data-phase'), 'running');
      assert.ok(await page.locator('#emailRevise').isDisabled());
      assert.match(await page.locator('.email-comparison').innerText(), /Read-only initial suggestion[\s\S]*Starts as a copy/);
      await page.locator('.email-review').screenshot({path: path.join(fixture.root, 'email-activity-revision.png')});
      Object.assign(activeDraft, {status: 'sending', approved_at: new Date().toISOString()});
      activity.operations = [{id: 'approve:' + activeDraft.id, state: 'completed'}, {id: 'finalize:' + activeDraft.id, state: 'running', started_at: started}];
      await page.evaluate(() => window.nexusEmail.refresh());
      assert.equal(await page.locator('#emailSendStatus').getAttribute('data-phase'), 'running');
      assert.ok(await page.locator('#emailApprove').isDisabled());
      await page.locator('.email-review').screenshot({path: path.join(fixture.root, 'email-activity-sending.png')});
      activeDraft.status = 'approved';
      activeDraft.error = 'An existing reply composer is open. Finish or close it before sending this reviewed reply.';
      activity.operations[1] = {id: 'finalize:' + activeDraft.id, state: 'failed', error: activeDraft.error};
      await page.evaluate(() => window.nexusEmail.refresh());
      assert.equal(await page.locator('#emailSendStatus').getAttribute('data-phase'), 'failed');
      assert.match(await page.locator('#emailSendStatus').innerText(), /Finish or close it/);
      assert.equal(await page.locator('#emailSendStatus .is-running').count(), 0);
      await page.locator('.email-review').screenshot({path: path.join(fixture.root, 'email-activity-send-error.png')});
      Object.assign(activeDraft, {status: 'sent', error: '', delivery_status: 'browser_confirmed'});
      activity.operations[1].state = 'completed';
      activity.operations[1].error = '';
      await page.evaluate(() => window.nexusEmail.refresh());
      assert.equal(await page.locator('#emailSendStatus').getAttribute('data-phase'), 'complete');
      assert.equal(await page.locator('#emailSendStatus .is-running').count(), 0);
    } finally {
      await page.unroute('**/api/email', activityRoute);
      await page.evaluate(() => window.nexusEmail.refresh());
    }
    assert.equal(await page.locator('#emailReply').inputValue(), exported.edited, 'Real synthetic fixture restored after transient activity checks');
    await page.locator(".email-setup summary").click();
    await page.locator("#emailView").screenshot({ path: path.join(fixture.root, "email-desktop.png") });
    await page.setViewportSize({ width: 760, height: 850 });
    const dimensions = await page.evaluate(() => ({
      width: document.documentElement.clientWidth, content: document.documentElement.scrollWidth,
    }));
    assert.ok(dimensions.content <= dimensions.width + 2, "Email panel fits a narrow window");
    await page.screenshot({ path: path.join(fixture.root, "email-narrow.png"), fullPage: true });
    console.log(JSON.stringify({ packaged_email: "passed", download: output,
      saved_edit_survived_restart: true, learned_memory_survived_restart: true,
      electron_activity_feedback: true,
      screenshots: ["email-desktop.png", "email-narrow.png"], real_mail_sent: false }));
  } finally {
    await app.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
