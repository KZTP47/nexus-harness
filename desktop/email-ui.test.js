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
const productionSource = fs.readFileSync(path.join(ui, 'email.js'), 'utf8');
// Shared readiness endpoint for the existing offline UI fixtures. Dedicated
// preflight tests below use productionSource and assert the complete sequence.
const source = `{
  const requestBeforePreparation = window.request;
  window.request = async (url, options) => {
    if (url.endsWith('/prepare_draft')) {
      const payload = JSON.parse(options.body);
      const draft = window.snapshot?.drafts?.find(d => d.id === payload.draft_id);
      if (draft) {
        if (draft.status === 'approved' && draft.error) { draft.status = 'review'; draft.error = ''; delete draft.approved_at; }
        return {ready: true, draft: structuredClone(draft)};
      }
    }
    return requestBeforePreparation(url, options);
  };
}
` + productionSource;

test('Polling settings distinguish unsaved and saved intervals, show scan timing and refresh at a short cadence', {skip: !executablePath, timeout: 45000}, async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const page = await browser.newPage(); await page.setContent('<main id="emailView"></main>');
    await page.evaluate(() => {
      window.now = 100000; Date.now = () => window.now;
      window.setInterval = callback => { window.pollUI = callback; return 1; };
      window.reads = 0; window.calls = [];
      window.snapshot = {accounts: [{id:'a',kind:'browser_outlook',provider_route:'assistant',poll_seconds:60,poll_enabled:true}],providers:[{id:'assistant',model:'model'}],messages:[],drafts:[],memories:[],polling:{contract:'email-polling/v1',accounts:[{account_id:'a',interval_seconds:60,enabled:true,scan_running:false,next_check_in_seconds:12,last_duration_seconds:18.2}]}};
      window.request = async (url, options) => {
        if (!options) { window.reads++; return structuredClone(window.snapshot); }
        const data = JSON.parse(options.body); window.calls.push(data);
        Object.assign(window.snapshot.accounts[0], data);
        Object.assign(window.snapshot.polling.accounts[0], {interval_seconds:data.poll_seconds,enabled:data.poll_enabled,next_check_in_seconds:3});
        return {account:structuredClone(window.snapshot.accounts[0])};
      };
    });
    await page.addScriptTag({content:source}); await page.evaluate(() => window.nexusEmail.refresh());
    await page.locator('#emailAutoSeconds').fill('3');
    assert.match(await page.locator('#emailPollingSettingsStatus').textContent(), /Unsaved changes.*60 seconds/);
    await page.locator('#emailSaveAssistant').click();
    await page.waitForFunction(() => document.querySelector('#emailPollingSettingsStatus').textContent === 'Saved check interval: 3 seconds.');
    assert.equal(await page.evaluate(() => window.calls[0].poll_seconds), 3);
    assert.match(await page.locator('#emailPollingTiming').textContent(), /Saved interval: 3s.*Last check took 18.2s/);
    const reads = await page.evaluate(() => window.reads);
    await page.evaluate(() => { window.now += 2999; window.pollUI(); });
    assert.equal(await page.evaluate(() => window.reads), reads);
    await page.evaluate(() => { window.now += 1; window.pollUI(); });
    await page.waitForFunction(count => window.reads === count + 1, reads);
    await page.evaluate(() => { window.snapshot.polling.accounts[0].scan_running = true; window.snapshot.polling.accounts[0].next_check_in_seconds = null; });
    await page.evaluate(() => window.nexusEmail.refresh());
    assert.match(await page.locator('#emailPollingTiming').textContent(), /check is in progress; checks do not overlap/);
    await page.locator('#emailAutoSeconds').fill('0'); await page.locator('#emailSaveAssistant').click();
    assert.equal(await page.evaluate(() => window.calls.length), 1);
    assert.match(await page.locator('#emailNotice').textContent(), /whole-number/);
    // Reloaded settings reflect durable data rather than the invalid input.
    await page.setContent('<main id="emailView"></main>'); await page.addScriptTag({content:source}); await page.evaluate(() => window.nexusEmail.refresh());
    assert.equal(await page.locator('#emailAutoSeconds').inputValue(), '3');
  } finally { await browser.close(); }
});

test('New arrivals lead the inbox list and a successful retry replaces its nearby failure', {skip: !executablePath, timeout: 45000}, async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const page = await browser.newPage(); await page.setContent('<main id="emailView"></main>');
    await page.evaluate(() => {
      window.snapshot = {accounts: [{id: 'a', kind: 'browser_outlook'}], providers: [], drafts: [], memories: [], messages: [
        {id: 'old', account_id: 'a', sender: 'older@example.test', subject: 'Old mail', body: 'Old', imported_at: '2025-01-01T10:00:00Z'},
        {id: 'new', account_id: 'a', sender: 'new@example.test', subject: '', body: 'New arrival', imported_at: '2025-01-01T11:00:00Z'},
        {id: 'history', account_id: 'a', sender: 'history@example.test', subject: 'Historical', body: 'History', received_at: '2024-12-01T11:00:00Z', imported_at: '2025-01-01T12:00:00Z'}
      ], operations: [{id: 'sync:a', state: 'failed', error: 'The inbox could not be read in time. Choose Check inbox now to retry.'}]};
      window.request = async () => structuredClone(window.snapshot);
    });
    await page.addScriptTag({content: source}); await page.evaluate(() => window.nexusEmail.refresh());
    assert.match(await page.locator('.email-message').first().textContent(), /\(No subject\).*new@example.test/s);
    assert.match(await page.locator('.email-message').last().textContent(), /Historical/);
    assert.match(await page.locator('#emailInboxStatus').textContent(), /Check inbox now/);
    await page.locator('.email-message').first().click();
    assert.match(await page.locator('#emailIncoming').textContent(), /New arrival/);
    await page.evaluate(() => { window.snapshot.operations[0] = {id: 'sync:a', state: 'completed', finished_at: '2025-01-01T12:00:00Z'}; });
    await page.evaluate(() => window.nexusEmail.refresh());
    assert.match(await page.locator('#emailInboxStatus').textContent(), /Inbox check completed at/);
    await page.evaluate(() => { window.snapshot.accounts[0].sync_has_more = true; });
    await page.evaluate(() => window.nexusEmail.refresh());
    assert.match(await page.locator('#emailInboxStatus').textContent(), /More inbox messages remain/);
    await page.evaluate(() => { window.snapshot.accounts[0].sync_has_more = false; });
    await page.evaluate(() => window.nexusEmail.refresh());
    assert.doesNotMatch(await page.locator('#emailInboxStatus').textContent(), /could not|retry/);
    assert.doesNotMatch(await page.locator('#emailOperations').textContent(), /Failed/);
  } finally { await browser.close(); }
});

test('Browser unknown delivery requires explicit current-revision verification and never sends again', {skip: !executablePath, timeout: 45000}, async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const page = await browser.newPage();
    await page.setContent('<main id="emailView" class="email-workspace"></main>');
    await page.addStyleTag({content: fs.readFileSync(path.join(ui, 'email.css'), 'utf8')});
    await page.evaluate(() => {
      window.calls = [];
      window.snapshot = {accounts: [{id: 'a', kind: 'browser_outlook'}], providers: [], messages: [{id: 'm', account_id: 'a', sender: 'person@example.test', subject: 'Request', body: 'Message'}], drafts: [{id: 'd', account_id: 'a', message_id: 'm', status: 'delivery_unknown', original: 'Reviewed reply', edited: 'Reviewed reply', revision: 2}], memories: []};
      window.request = async (url, options) => {
        if (!options) return structuredClone(window.snapshot);
        const data = JSON.parse(options.body); window.calls.push({url, data});
        if (url.endsWith('confirm_browser_delivery')) { Object.assign(window.snapshot.drafts[0], {status: 'sent', delivery_status: 'user_confirmed'}); return {draft: window.snapshot.drafts[0]}; }
        throw new Error('Unexpected delivery action');
      };
    });
    await page.addScriptTag({content: source}); await page.evaluate(() => window.nexusEmail.refresh());
    await page.locator('.email-message').click();
    assert.equal(await page.locator('#emailManualDeliveryConfirmation').isVisible(), true);
    assert.equal(await page.locator('#emailRecordVerifiedSend').isDisabled(), true);
    assert.match(await page.locator('#emailManualDeliveryConfirmation').textContent(), /I checked Sent Items or the recipient confirmed this reply arrived/);
    await page.locator('#emailVerifiedSendChecked').check();
    assert.equal(await page.locator('#emailRecordVerifiedSend').isDisabled(), false);
    await page.evaluate(() => { window.snapshot.drafts[0].revision = 3; });
    await page.evaluate(() => window.nexusEmail.refresh());
    assert.equal(await page.locator('#emailVerifiedSendChecked').isChecked(), false);
    assert.equal(await page.locator('#emailRecordVerifiedSend').isDisabled(), true);
    await page.locator('#emailVerifiedSendChecked').check();
    await page.locator('#emailRecordVerifiedSend').click();
    await page.waitForFunction(() => document.querySelector('#emailDraftStatus').textContent.includes('You verified'));
    assert.deepEqual(await page.evaluate(() => window.calls), [{url: '/api/email/confirm_browser_delivery', data: {account_id: 'a', draft_id: 'd', revision: 3, confirmation_contract: 'browser-delivery-confirmation/v1'}}]);
    assert.match(await page.locator('#emailSendStatus').textContent(), /You verified this reply was sent/);
    assert.equal(await page.locator('#emailManualDeliveryConfirmation').isVisible(), false);
    await page.evaluate(() => { window.snapshot.drafts[0].status = 'delivery_unknown'; window.snapshot.accounts[0].kind = 'imap'; });
    await page.evaluate(() => window.nexusEmail.refresh());
    assert.equal(await page.locator('#emailManualDeliveryConfirmation').isVisible(), false);
    assert.equal(await page.locator('#emailRecordVerifiedSend').isDisabled(), true);
  } finally { await browser.close(); }
});

test('Draft controls distinguish queue, live callback, stale activity, send failure and accepted outcome', {skip: !executablePath, timeout: 45000}, async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const page = await browser.newPage();
    await page.setContent('<main id="emailView" class="email-workspace"></main>');
    await page.addStyleTag({content: fs.readFileSync(path.join(ui, 'styles.css'), 'utf8')});
    await page.addStyleTag({content: fs.readFileSync(path.join(ui, 'email.css'), 'utf8')});
    await page.evaluate(() => {
      window.calls = [];
      window.snapshot = {accounts: [{id: 'a', kind: 'browser_outlook'}], providers: [], messages: [{id: 'm', account_id: 'a', sender: 'person@example.test', subject: 'Request', body: 'A request'}], drafts: [{id: 'd', account_id: 'a', message_id: 'm', status: 'queued', original: '', edited: '', revision: 0, execution_id: 'technical-workflow-identifier'}], memories: [], operations: [{id: 'draft:d', state: 'completed'}]};
      window.request = async (_url, options) => { if (options) window.calls.push(JSON.parse(options.body)); return structuredClone(window.snapshot); };
    });
    await page.addScriptTag({content: source}); await page.evaluate(() => window.nexusEmail.refresh());
    await page.locator('.email-message').click();
    assert.equal(await page.locator('#emailGenerateStatus').getAttribute('data-phase'), 'queued');
    assert.equal(await page.locator('#emailGenerateStatus .is-running').count(), 0);
    assert.doesNotMatch(await page.locator('#emailDraftStatus').textContent(), /technical-workflow-identifier/);
    await page.evaluate(() => { window.snapshot.drafts[0].status = 'generating'; window.snapshot.operations.push({id: 'generate:d', state: 'running', started_at: new Date(Date.now() - 9000).toISOString()}); });
    await page.evaluate(() => window.nexusEmail.refresh());
    assert.equal(await page.locator('#emailGenerateStatus').getAttribute('data-phase'), 'running');
    assert.match(await page.locator('#emailGenerateStatus').textContent(), /Last reported.*\ds elapsed/);
    assert.equal(await page.locator('#emailGenerate').isDisabled(), true);
    assert.equal(await page.locator('#emailGenerateStatus .is-running').evaluate(node => getComputedStyle(node).animationName), 'email-activity-turn');
    await page.emulateMedia({reducedMotion: 'reduce'});
    assert.equal(await page.locator('#emailGenerateStatus .is-running').evaluate(node => getComputedStyle(node).animationName), 'none');
    await page.evaluate(() => { window.snapshot.captured_at = new Date(Date.now() - 60000).toISOString(); });
    await page.evaluate(() => window.nexusEmail.refresh());
    assert.equal(await page.locator('#emailGenerateStatus').getAttribute('data-phase'), 'stale');
    assert.equal(await page.locator('#emailGenerateStatus .is-running').count(), 0);
    await page.evaluate(() => { delete window.snapshot.captured_at; Object.assign(window.snapshot.drafts[0], {status: 'review', original: 'Initial suggestion', edited: 'Initial suggestion', revision: 1}); window.snapshot.operations = [{id: 'generate:d', state: 'completed'}]; });
    await page.evaluate(() => window.nexusEmail.refresh());
    assert.equal(await page.locator('#emailGenerateStatus').getAttribute('data-phase'), 'complete');
    assert.equal(await page.locator('#emailReply').inputValue(), 'Initial suggestion');
    assert.match(await page.locator('.email-comparison').textContent(), /Read-only initial suggestion.*Starts as a copy/s);
    await page.locator('#emailReply').fill('My unsaved correction');
    await page.evaluate(() => { window.snapshot.operations = [{id: 'revise:d', state: 'failed', error: 'The AI connection stopped. Try revision again.'}]; });
    await page.evaluate(() => window.nexusEmail.refresh());
    assert.equal(await page.locator('#emailReply').inputValue(), 'My unsaved correction');
    assert.match(await page.locator('#emailRevisionStatus').textContent(), /AI connection stopped/);
    await page.locator('#emailRevert').click();
    await page.evaluate(() => { window.snapshot.drafts[0].status = 'approved'; window.snapshot.drafts[0].approved_at = new Date().toISOString(); window.snapshot.drafts[0].error = 'An existing reply composer is open. Finish or close it before sending this reviewed reply.'; window.snapshot.operations = [{id: 'approve:d', state: 'completed'}, {id: 'finalize:d', state: 'failed', error: window.snapshot.drafts[0].error}]; });
    await page.evaluate(() => window.nexusEmail.refresh());
    assert.equal(await page.locator('#emailSendStatus').getAttribute('data-phase'), 'failed');
    assert.match(await page.locator('#emailSendStatus').textContent(), /Finish or close it/);
    // A live retry can wait on the browser lock while retaining the preceding
    // error on the approved draft. Current callback activity takes precedence.
    await page.evaluate(() => { window.snapshot.operations[1] = {id: 'finalize:d', state: 'running', started_at: new Date().toISOString()}; });
    await page.evaluate(() => window.nexusEmail.refresh());
    assert.equal(await page.locator('#emailSendStatus').getAttribute('data-phase'), 'running');
    assert.equal(await page.locator('#emailSendStatus .is-running').count(), 1);
    assert.doesNotMatch(await page.locator('#emailSendStatus').textContent(), /Finish or close it/);
    assert.equal(await page.locator('#emailResume').isDisabled(), true);
    await page.evaluate(() => { Object.assign(window.snapshot.operations[1], {state: 'failed', error: 'Retry failed: close the existing composer.'}); });
    await page.evaluate(() => window.nexusEmail.refresh());
    assert.equal(await page.locator('#emailSendStatus').getAttribute('data-phase'), 'failed');
    assert.equal(await page.locator('#emailSendStatus .is-running').count(), 0);
    assert.match(await page.locator('#emailSendStatus').textContent(), /Retry failed/);
    await page.evaluate(() => { window.snapshot.drafts[0].error = ''; window.snapshot.drafts[0].status = 'sending'; window.snapshot.operations[1] = {id: 'finalize:d', state: 'running', started_at: new Date().toISOString()}; });
    await page.evaluate(() => window.nexusEmail.refresh());
    assert.equal(await page.locator('#emailSendStatus').getAttribute('data-phase'), 'running');
    await page.evaluate(() => { document.querySelector('#emailApprove').click(); document.querySelector('#emailComposeSend').click(); document.querySelector('#emailComposeSend').click(); });
    assert.equal(await page.evaluate(() => window.calls.length), 0);
    await page.evaluate(() => { window.snapshot.drafts[0].status = 'delivery_unknown'; });
    await page.evaluate(() => window.nexusEmail.refresh());
    assert.equal(await page.locator('#emailSendStatus').getAttribute('data-phase'), 'unknown');
    assert.equal(await page.locator('#emailSendStatus .is-running').count(), 0);
    await page.setViewportSize({width: 390, height: 900});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.evaluate(() => { window.snapshot.drafts[0].status = 'sent'; window.snapshot.drafts[0].delivery_status = 'browser_confirmed'; });
    await page.evaluate(() => window.nexusEmail.refresh());
    assert.equal(await page.locator('#emailSendStatus').getAttribute('data-phase'), 'complete');
    assert.equal(await page.locator('#emailSendStatus .is-running').count(), 0);
    assert.equal(await page.locator('#emailApprove').isDisabled(), true);
  } finally { await browser.close(); }
});

test('EmailEngine service setup protects token, selects discovered account, and uses send and delivery actions', {skip: !executablePath, timeout: 45000}, async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const page = await browser.newPage();
    await page.setContent('<main id="emailView"></main>');
    await page.evaluate(() => {
      window.calls = [];
      window.snapshot = {accounts: [], providers: [{id: 'route', name: 'Assistant', model: 'model-test'}], emailengine: {configured: false}, messages: [], drafts: [], memories: []};
      window.request = async (url, options) => {
        if (!options) return structuredClone(window.snapshot);
        const data = JSON.parse(options.body); window.calls.push({url, data});
        if (url.endsWith('emailengine_configure')) { window.snapshot.emailengine = {configured: true, url: data.url}; return {accounts: [{account: 'remote-a', email: 'mailbox@example.test', state: 'connected'}]}; }
        if (url.endsWith('emailengine_accounts')) return {accounts: [{id: 'remote-a', name: 'Service mailbox'}]};
        if (url.endsWith('emailengine_connect')) { const account = {id: 'a', kind: 'emailengine', name: 'Service mailbox', connection_state: 'connected', provider_route: 'route'}; window.snapshot.accounts = [account]; window.snapshot.messages = [{id: 'm', account_id: 'a', sender: 'person@example.test', subject: 'A request', body: 'Help'}]; window.snapshot.drafts = [{id: 'd', account_id: 'a', message_id: 'm', status: 'review', original: 'A reply', edited: 'A reply', revision: 1}]; return {account}; }
        if (url.endsWith('approve_draft')) window.snapshot.drafts[0].status = 'submitted';
        if (url.endsWith('check_delivery')) window.snapshot.drafts[0].status = 'delivery_unknown';
        return {};
      };
    });
    await page.addScriptTag({content: source}); await page.evaluate(() => window.nexusEmail.refresh());
    await page.locator('#emailConnectionMethod').selectOption('emailengine');
    assert.match(await page.locator('#emailManagedService').textContent(), /Local setup downloads verified EmailEngine and Redis files/);
    assert.equal(await page.locator('#emailLocalCard-browser_outlook').isVisible(), false);
    await page.locator('#emailManagedService summary').click();
    await page.locator('#emailManagedUrl').fill('https://mail-service.example.test');
    await page.locator('#emailManagedToken').fill('synthetic-token');
    assert.equal(await page.locator('#emailManagedToken').getAttribute('type'), 'password');
    await page.locator('#emailManagedService button[type=submit]').click();
    await page.waitForFunction(() => document.querySelector('#emailManagedToken').value === '');
    await page.locator('#emailManagedAccounts').selectOption('remote-a');
    await page.locator('#emailManagedConnect').click();
    await page.waitForFunction(() => document.querySelector('#emailAccount').value === 'a');
    const connection = await page.evaluate(() => window.calls.find(c => c.url.endsWith('emailengine_connect')).data);
    assert.deepEqual(connection, {remote_account_id: 'remote-a', provider_route: 'route', provider_model: 'model-test', poll_enabled: true, poll_seconds: 60});
    assert.equal(await page.locator('#emailReconnect').isVisible(), false);
    assert.equal(await page.locator('#emailDisconnect').isVisible(), true);
    assert.equal(await page.locator('#emailSync').isDisabled(), false);
    await page.locator('.email-message').click();
    assert.equal(await page.locator('#emailApprove').textContent(), 'Review & send reply');
    await page.locator('#emailApprove').click();
    await page.locator('#emailComposeSend').click();
    await page.waitForFunction(() => document.querySelector('#emailDraftStatus').textContent.includes('Queued with'));
    assert.equal(await page.locator('#emailApprove').isDisabled(), true);
    await page.locator('#emailCheckDelivery').click();
    await page.waitForFunction(() => document.querySelector('#emailDraftStatus').textContent.includes('uncertain'));
    assert.deepEqual(await page.evaluate(() => window.calls.find(c => c.url.endsWith('check_delivery')).data), {account_id: 'a', draft_id: 'd'});
    await page.evaluate(() => { window.snapshot.drafts[0].status = 'sent'; window.snapshot.memories = [{id: 'old', account_id: 'a', status: 'superseded', text: 'Obsolete'}, {id: 'active', account_id: 'a', status: 'active', text: 'Current preference', source_draft_id: 'd', source_revision: 2, revision: 3}]; });
    await page.evaluate(() => window.nexusEmail.refresh());
    assert.match(await page.locator('#emailDraftStatus').textContent(), /recipient delivery is not confirmed/);
    assert.equal(await page.locator('#emailCheckDelivery').isVisible(), false);
    assert.equal(await page.locator('#emailMemories textarea').count(), 1);
    assert.match(await page.locator('#emailMemories small').textContent(), /draft d, source revision 2.*Preference revision 3.*active/);
    await page.locator('#emailDisconnect').click();
    await page.waitForFunction(() => window.calls.some(c => c.url.endsWith('oauth_disconnect')));
  } finally { await browser.close(); }
});

test('Local EmailEngine preparation submits no credentials and progresses asynchronously to ready', {skip: !executablePath, timeout: 45000}, async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const page = await browser.newPage();
    await page.setContent('<main id="emailView"></main>');
    await page.evaluate(() => {
      window.calls = [];
      window.snapshot = {accounts: [], providers: [], messages: [], drafts: [], memories: [], emailengine: {configured: false, local_runtime: {state: 'unavailable', message: 'Local mailbox service is not prepared.'}}};
      window.request = async (url, options) => {
        if (!options) return structuredClone(window.snapshot);
        const data = JSON.parse(options.body); window.calls.push({url, data});
        if (url.endsWith('emailengine_prepare')) return new Promise(resolve => { window.finishPrepareRequest = () => {
          window.snapshot.emailengine.local_runtime = {state: 'preparing', message: 'Preparing private mailbox service files.'};
          resolve({operation_id: 'setup-operation', state: 'running'});
        }; });
        return {};
      };
    });
    await page.addScriptTag({content: source}); await page.evaluate(() => window.nexusEmail.refresh());
    await page.locator('#emailConnectionMethod').selectOption('emailengine');
    await page.locator('#emailManagedPrepare').click();
    await page.waitForFunction(() => typeof window.finishPrepareRequest === 'function');
    assert.equal(await page.locator('#emailManagedPrepare').isDisabled(), true);
    assert.equal(await page.locator('#emailManagedSignIn').isDisabled(), true);
    assert.deepEqual(await page.evaluate(() => window.calls), [{url: '/api/email/emailengine_prepare', data: {}}]);
    await page.evaluate(() => window.finishPrepareRequest());
    await page.waitForFunction(() => document.querySelector('#emailManagedStatus').textContent.includes('Preparing private'));
    assert.equal(await page.locator('#emailManagedToken').inputValue(), '');
    await page.evaluate(() => { window.snapshot.emailengine = {configured: true, url: 'http://127.0.0.1:45671', local_runtime: {state: 'ready', message: 'Private mailbox service is ready.'}}; });
    await page.evaluate(() => window.nexusEmail.refresh());
    assert.equal(await page.locator('#emailManagedStatus').textContent(), 'Private mailbox service is ready.');
    assert.equal(await page.locator('#emailManagedUrl').inputValue(), 'http://127.0.0.1:45671');
    assert.equal(await page.locator('#emailManagedToken').inputValue(), '');
    assert.equal(await page.locator('#emailManagedSignIn').isDisabled(), false);
    assert.equal(await page.locator('#emailManagedToken').getAttribute('type'), 'password');
  } finally { await browser.close(); }
});

test('EmailEngine hosted sign-in offers a same-origin fallback and rejects unsafe returned links', {skip: !executablePath, timeout: 45000}, async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    // Synthetic URL credentials exercise rejection without resembling a saved login.
    const credentialUrl = new URL('https://service.example.test/accounts/new');
    credentialUrl.username = 'example-user';
    credentialUrl.password = 'example-password';
    for (const authorizationUrl of ['https://service.example.test/accounts/new?data=signed-link', 'https://unexpected.example.test/accounts/new', credentialUrl.href, 'javascript:alert(1)']) {
      const page = await browser.newPage();
      await page.setContent('<main id="emailView"></main>');
      await page.evaluate(url => {
        window.calls = [];
        window.snapshot = {accounts: [], providers: [], messages: [], drafts: [], memories: [], emailengine: {configured: true, url: 'https://service.example.test'}};
        window.request = async (path, options) => {
          if (!options) return structuredClone(window.snapshot);
          const data = JSON.parse(options.body); window.calls.push({path, data});
          return {authorization_url: url};
        };
      }, authorizationUrl);
      await page.addScriptTag({content: source}); await page.evaluate(() => window.nexusEmail.refresh());
      await page.locator('#emailConnectionMethod').selectOption('emailengine');
      await page.locator('#emailManagedSignIn').click();
      await page.waitForFunction(() => !document.querySelector('#emailManagedSignIn').disabled);
      assert.deepEqual(await page.evaluate(() => window.calls), [{path: '/api/email/emailengine_sign_in', data: {}}]);
      const link = page.locator('#emailManagedSignInLink');
      if (authorizationUrl.includes('?data=signed-link')) {
        assert.equal(await link.isVisible(), true);
        assert.equal(await link.getAttribute('href'), authorizationUrl);
        assert.equal(await link.getAttribute('target'), '_blank');
        assert.equal(await link.getAttribute('rel'), 'noopener noreferrer');
        assert.match(await page.locator('#emailNotice').textContent(), /Finish adding your mailbox/);
      } else {
        assert.equal(await link.isVisible(), false);
        assert.equal(await link.getAttribute('href'), null);
        assert.match(await page.locator('#emailNotice').textContent(), /unexpected sign-in address/);
      }
      await page.close();
    }
  } finally { await browser.close(); }
});

test('Quarantined message summaries are escaped and scoped to the current mailbox fingerprint', {skip: !executablePath, timeout: 45000}, async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const page = await browser.newPage();
    await page.setContent('<main id="emailView"></main>');
    await page.evaluate(() => {
      window.snapshot = {accounts: [{id: 'a', name: 'Mailbox A', kind: 'emailengine', fingerprint: 'fp-a'}, {id: 'b', name: 'Mailbox B', kind: 'emailengine', fingerprint: 'fp-b'}], providers: [], messages: [], drafts: [], memories: [],
        failed_imports: [
          {account_id: 'a', account_fingerprint: 'fp-a', error: 'Oversized message <img src=x onerror="window.injected=true">'},
          {account_id: 'a', account_fingerprint: 'obsolete', error: 'Obsolete connection failure'},
          {account_id: 'b', account_fingerprint: 'fp-b', error: 'Mailbox B private failure'}]};
      window.request = async () => structuredClone(window.snapshot);
    });
    await page.addScriptTag({content: source}); await page.evaluate(() => window.nexusEmail.refresh());
    await page.locator('#emailAccount').selectOption('a');
    let summary = await page.locator('#emailQueue').textContent();
    assert.match(summary, /Message could not be imported: Oversized message/);
    assert.match(summary, /retry on a later inbox scan/);
    assert.doesNotMatch(summary, /Obsolete connection|Mailbox B private/);
    assert.equal(await page.locator('#emailQueue img').count(), 0);
    assert.equal(await page.evaluate(() => !!window.injected), false);
    await page.locator('#emailAccount').selectOption('b');
    summary = await page.locator('#emailQueue').textContent();
    assert.match(summary, /Mailbox B private failure/);
    assert.doesNotMatch(summary, /Oversized message|Obsolete connection/);
  } finally { await browser.close(); }
});

test('Email review escapes mail, preserves edits during refresh, and has a distinct one-off approval', {skip: !executablePath, timeout: 45000}, async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const page = await browser.newPage();
    await page.setContent('<button data-view="email">Email assistant</button><main id="emailView"></main>');
    await page.evaluate(() => {
      window.calls = [];
      window.snapshot = {accounts: [{id: 'mail-a', name: 'Example mailbox', email: 'me@example.test', kind: 'import', provider_route: 'codex'}], providers: [{id: 'codex', name: 'Codex'}], messages: [{id: 'message-a', account_id: 'mail-a', sender: 'sender@example.test', subject: '<img src=x onerror="window.injected=true">', body: '<script>window.injected=true</script>'}], drafts: [{id: 'draft-a', account_id: 'mail-a', message_id: 'message-a', status: 'review', original: 'A formal response.', edited: 'A formal response.', revision: 1}], memories: [], orchestration: {status: 'stopped'}};
      window.request = async (url, options) => {
        if (!options) return structuredClone(window.snapshot);
        const data = JSON.parse(options.body); window.calls.push({url, data});
        if (url.endsWith('save_draft')) { window.snapshot.drafts[0].edited = data.text; window.snapshot.drafts[0].revision += 1; }
        if (url.endsWith('approve_draft')) window.snapshot.drafts[0].status = 'approved';
        return {};
      };
    });
    await page.addScriptTag({content: source});
    await page.evaluate(() => window.nexusEmail.refresh());
    await page.locator('.email-message').click();
    assert.match(await page.locator('#emailIncoming').textContent(), /<script>/);
    assert.equal(await page.evaluate(() => !!window.injected), false);
    await page.locator('#emailReply').fill('Thanks, I can help.');
    await page.evaluate(() => { window.snapshot.drafts[0].edited = 'Background stale value'; });
    await page.evaluate(() => window.nexusEmail.refresh());
    assert.equal(await page.locator('#emailReply').inputValue(), 'Thanks, I can help.');
    assert.match(await page.locator('#emailDiff').textContent(), /Unsaved edits/);
    await page.locator('#emailSave').click();
    await page.waitForFunction(() => window.calls.some(c => c.url.endsWith('save_draft')));
    await page.waitForFunction(() => !document.querySelector('#emailApprove').disabled);
    await page.locator('#emailLearn').uncheck();
    await page.locator('#emailApprove').click();
    await page.locator('#emailComposeSend').click();
    await page.waitForFunction(() => window.calls.some(c => c.url.endsWith('approve_draft')));
    const approval = await page.evaluate(() => window.calls.find(c => c.url.endsWith('approve_draft')).data);
    assert.equal(approval.learn, false);
    assert.equal(approval.text, 'Thanks, I can help.');
    assert.equal(approval.revision, 2);
    assert.equal(approval.account_id, 'mail-a');
    assert.match(await page.locator('#emailEngineStatus').textContent(), /stopped/);
  } finally { await browser.close(); }
});

test('Email inbox changes are blocked while dirty; readiness notices deduplicate and memory stays scoped', {skip: !executablePath, timeout: 45000}, async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const page = await browser.newPage();
    await page.setContent('<button data-view="email">Email assistant</button><main id="emailView"></main>');
    await page.evaluate(() => {
      window.setInterval = callback => { window.emailPoll = callback; return 1; };
      window.snapshot = {accounts: [{id: 'a', name: 'A', kind: 'import'}], providers: [], messages: [{id: 'm1', account_id: 'a', sender: 'one@example.test', subject: 'First', body: 'One'}, {id: 'm2', account_id: 'a', sender: 'two@example.test', subject: 'Second', body: 'Two'}], drafts: [{id: 'd1', account_id: 'a', message_id: 'm1', status: 'review', original: 'One', edited: 'One', revision: 1}], memories: [{id: 'mem1', account_id: 'a', text: 'Use short replies'}, {id: 'mem2', account_id: 'other', text: 'Other mailbox private preference'}], orchestration: {status: 'unavailable', error: 'Runtime missing'}, operations: [{id: 'draft:private-running-id', state: 'running'}, {id: 'sync:private-mailbox-id', state: 'failed', error: 'Check your IMAP password, then retry.'}, {id: 'recovery:private-completed-id', state: 'completed'}, {id: 'approve:private-approval-id', state: 'running'}, {id: 'engine', state: 'running'}, {id: 'learning:private-learning-id', state: 'running'}]};
      window.request = async () => structuredClone(window.snapshot);
    });
    await page.addScriptTag({content: source});
    await page.evaluate(() => window.nexusEmail.refresh());
    await page.locator('.email-message').first().click();
    await page.locator('#emailReply').fill('Unsaved');
    await page.locator('.email-message').nth(1).click();
    assert.equal(await page.locator('#emailReply').inputValue(), 'Unsaved');
    assert.match(await page.locator('#emailNotice').textContent(), /Save or discard/);
    assert.equal(await page.locator('#emailMemories textarea').count(), 1);
    assert.match(await page.locator('#emailEngineStatus').textContent(), /Runtime missing/);
    const operations = await page.locator('#emailOperations').textContent();
    for (const label of ['Creating draft', 'Checking inbox', 'Resuming approved reply', 'Starting Kestra', 'Learning preferences']) assert.ok(operations.includes(label), label);
    assert.match(operations, /Check your IMAP password, then retry/);
    assert.doesNotMatch(operations, /private-|completed|Checking interrupted workflow/);
    assert.equal(await page.locator('#emailOperations .email-error').count(), 1);
    await page.evaluate(() => { window.snapshot.drafts.push({id: 'd2', account_id: 'a', message_id: 'm2', status: 'review'}); document.querySelector('#emailView').hidden = true; const now = Date.now(); Date.now = () => now + 6000; window.emailPoll(); });
    await page.waitForFunction(() => document.querySelector('[data-view="email"]').textContent === 'Email assistant (2 drafts)');
    assert.equal(await page.locator('#emailView').isVisible(), false);
    assert.match(await page.locator('#emailNotice').textContent(), /1 new draft/);
    await page.evaluate(() => { document.querySelector('#emailNotice').textContent = 'Read'; });
    await page.evaluate(() => window.nexusEmail.refresh());
    assert.equal(await page.locator('#emailNotice').textContent(), 'Read');
  } finally { await browser.close(); }
});

test('Import cannot mix incoming mail with another draft or race a pending mutation', {skip: !executablePath, timeout: 45000}, async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const page = await browser.newPage();
    await page.setContent('<main id="emailView"></main>');
    await page.evaluate(() => {
      window.calls = [];
      window.snapshot = {accounts: [{id: 'a', name: 'A', kind: 'import'}], providers: [{id: 'route-1', name: 'Codex'}], messages: [{id: 'm1', account_id: 'a', sender: 'one@example.test', subject: 'First', body: 'One'}], drafts: [{id: 'd1', account_id: 'a', message_id: 'm1', status: 'review', original: 'Reply to first', edited: 'Reply to first', revision: 1}], memories: []};
      window.request = async (url, options) => {
        if (!options) { const snapshot = structuredClone(window.snapshot); if (window.holdSnapshot) { window.holdSnapshot = false; return new Promise(resolve => { window.finishSnapshot = () => resolve(snapshot); }); } return snapshot; }
        window.calls.push({url, data: JSON.parse(options.body)});
        if (url.endsWith('/import')) return await new Promise(resolve => { window.finishImport = () => { const message = {id: 'm2', account_id: 'a', sender: 'two@example.test', subject: 'Second', body: 'Two'}; window.snapshot.messages.push(message); resolve({message}); }; });
        return {};
      };
    });
    await page.addScriptTag({content: source});
    await page.evaluate(() => window.nexusEmail.refresh());
    await page.locator('.email-message').click();
    await page.locator('#emailReply').fill('My unsaved reply to first');
    await page.locator('.email-import summary').click();
    await page.locator('#emailSender').fill('two@example.test');
    await page.locator('#emailSubject').fill('Second');
    await page.locator('#emailBody').fill('Two');
    await page.locator('.email-import button').click();
    assert.equal(await page.evaluate(() => window.calls.length), 0);
    assert.match(await page.locator('#emailNotice').textContent(), /Save or revert/);
    await page.locator('#emailRevert').click();
    await page.evaluate(() => { window.holdSnapshot = true; void window.nexusEmail.refresh(); });
    await page.locator('.email-import button').click();
    await page.waitForFunction(() => !!window.finishImport);
    await page.locator('#emailNewAccount').click();
    assert.equal(await page.locator('#emailAccount').inputValue(), 'a');
    await page.locator('.email-message').click();
    assert.match(await page.locator('#emailNotice').textContent(), /Wait for the current action/);
    await page.evaluate(() => { window.finishImport(); window.finishSnapshot(); });
    await page.waitForFunction(() => document.querySelector('#emailIncoming').textContent.includes('Second'));
    assert.equal(await page.locator('#emailOriginal').textContent(), '');
    assert.equal(await page.locator('#emailReply').inputValue(), '');
    assert.equal(await page.locator('#emailApprove').isDisabled(), true);
    assert.equal(await page.locator('#emailSender').inputValue(), '');
    assert.equal(await page.locator('#emailProvider').inputValue(), 'route-1');
  } finally { await browser.close(); }
});

test('Email export awaits the desktop bridge contract and reports save, cancellation, and rejection honestly', {skip: !executablePath, timeout: 45000}, async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const page = await browser.newPage();
    await page.setContent('<main id="emailView"></main>');
    // This is an isolated renderer contract test. Actual Electron IPC and disk
    // saving are verified by the packaged-app acceptance test separately.
    await page.evaluate(() => {
      window.nativeCalls = [];
      window.snapshot = {accounts: [{id: 'a', name: 'A', kind: 'import'}], providers: [], messages: [{id: 'm', account_id: 'a', sender: 'sender@example.test', subject: 'Export', body: 'Please reply'}], drafts: [{id: 'd', account_id: 'a', message_id: 'm', status: 'exported', original: 'Reply', edited: 'Reply', revision: 1}], memories: []};
      window.request = async (_url, options) => options ? {filename: 'reviewed-reply.eml', content: 'Subject: Re: Export\r\n\r\nReply'} : structuredClone(window.snapshot);
      window.harnessDesktop = {saveEmailFile: (filename, content) => new Promise((resolve, reject) => { window.nativeCalls.push({filename, content}); window.finishSave = resolve; window.failSave = reject; })};
    });
    await page.addScriptTag({content: source});
    await page.evaluate(() => window.nexusEmail.refresh());
    await page.locator('.email-message').click();
    await page.locator('#emailDownload').click();
    await page.waitForFunction(() => window.nativeCalls.length === 1);
    assert.equal(await page.locator('#emailDownload').isDisabled(), true);
    assert.equal(await page.locator('#emailNotice').textContent(), 'Working…');
    await page.evaluate(() => window.finishSave({saved: true, path: '/portable/exports/reviewed-reply.eml'}));
    await page.waitForFunction(() => !document.querySelector('#emailDownload').disabled);
    assert.match(await page.locator('#emailNotice').textContent(), /Reply saved to \/portable\/exports\/reviewed-reply\.eml/);
    const first = await page.evaluate(() => window.nativeCalls[0]);
    assert.equal(first.filename, 'reviewed-reply.eml');
    assert.equal(first.content, 'Subject: Re: Export\r\n\r\nReply');
    await page.locator('#emailDownload').click();
    await page.waitForFunction(() => window.nativeCalls.length === 2);
    await page.evaluate(() => window.finishSave({saved: false}));
    await page.waitForFunction(() => !document.querySelector('#emailDownload').disabled);
    assert.equal(await page.locator('#emailNotice').textContent(), 'Save cancelled. No file was saved.');
    await page.locator('#emailDownload').click();
    await page.waitForFunction(() => window.nativeCalls.length === 3);
    await page.evaluate(() => window.failSave(new Error('Destination is read-only.')));
    await page.waitForFunction(() => !document.querySelector('#emailDownload').disabled);
    assert.equal(await page.locator('#emailNotice').textContent(), 'Could not save reply: Destination is read-only.');
    assert.equal(await page.locator('#emailNotice.email-error').count(), 1);
  } finally { await browser.close(); }
});

test('Outlook and Gmail setup exposes real registration and sign-in states without inventing a connection', {skip: !executablePath, timeout: 45000}, async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const page = await browser.newPage();
    await page.setContent('<main id="emailView"></main>');
    // OAuth responses are a renderer contract fixture, not live mailbox evidence.
    await page.evaluate(() => {
      window.calls = [];
      window.snapshot = {accounts: [], providers: [{id: 'codex-work', name: 'Codex', model: 'model'}], messages: [], drafts: [], memories: [], oauth: {outlook: {configured: false, status: 'setup_required'}, gmail: {configured: false}, pending: []}};
      window.request = async (url, options) => {
        if (!options) return structuredClone(window.snapshot);
        const data = JSON.parse(options.body); window.calls.push({url, data});
        if (url.endsWith('oauth_configure')) { window.snapshot.oauth[data.provider].configured = true; return {}; }
        if (url.endsWith('oauth_auto_connect')) { if (!window.snapshot.oauth[data.provider].configured) return {state: 'publisher_registration_required', registration: {provider: data.provider, source: 'unconfigured', client_id: '', tenant: 'common'}, message: 'Sign-in needs an app registration.'}; const id = 'request-' + window.calls.length; window.snapshot.oauth.pending.push({id, provider: data.provider, state: 'pending', message: 'Waiting for browser sign-in.'}); return {state: 'authorization_pending', registration: {provider: data.provider, source: 'project_config', client_id: 'detected-client-id', tenant: 'common'}, request_id: id, authorization_url: data.provider === 'outlook' ? 'https://login.microsoftonline.com/common/oauth2/v2.0/authorize?state=fixture' : 'https://accounts.google.com/o/oauth2/v2/auth?state=fixture'}; }
        if (url.endsWith('oauth_disconnect')) window.snapshot.accounts[0].connection_state = 'disconnected';
        return {};
      };
    });
    await page.addScriptTag({content: source});
    await page.evaluate(() => window.nexusEmail.refresh());
    assert.equal(await page.locator('.email-setup').getAttribute('open'), null);
    assert.equal(await page.locator('.email-registrations').getAttribute('open'), null);
    assert.match(await page.locator('.email-connect').textContent(), /Incoming message context and approved edits are sent to your selected AI provider/);
    await page.locator('#emailConnectionMethod').selectOption('api');
    await page.locator('#emailAutoConnect-outlook').click();
    assert.equal(await page.evaluate(() => window.calls.length), 1);
    assert.match(await page.locator('#emailNotice').textContent(), /needs an app registration/);
    assert.equal(await page.locator('#emailClientId-outlook').isVisible(), true);
    assert.equal(await page.locator('#emailClientId-outlook').inputValue(), '');
    await page.locator('#emailClientId-outlook').fill('registered-client-id');
    await page.getByRole('button', {name: 'Save Outlook registration'}).click();
    await page.waitForFunction(() => !document.querySelector('#emailAutoConnect-outlook').disabled);
    await page.locator('#emailConnectionMethod').selectOption('api');
    await page.locator('#emailAutoConnect-outlook').click();
    await page.waitForFunction(() => document.querySelector('#emailOAuthLink-outlook').hasAttribute('href'));
    assert.match(await page.locator('#emailOAuthStatus-outlook').textContent(), /Waiting for browser sign-in/);
    assert.equal(await page.locator('#emailClientId-outlook').inputValue(), 'detected-client-id');
    await page.locator('#emailClientId-outlook').fill('unfinished-new-client-id');
    await page.evaluate(() => { window.snapshot.oauth.outlook.client_id = 'detected-client-id'; });
    await page.evaluate(() => window.nexusEmail.refresh());
    assert.equal(await page.locator('#emailClientId-outlook').inputValue(), 'unfinished-new-client-id');
    assert.doesNotMatch(await page.locator('#emailConnectionStatus').textContent(), /Connected/);
    const start = await page.evaluate(() => window.calls.filter(c => c.url.endsWith('oauth_auto_connect')).at(-1).data);
    assert.deepEqual(start, {provider: 'outlook', provider_route: 'codex-work', provider_model: 'model', poll_enabled: true, poll_seconds: 300});
    await page.evaluate(() => { window.snapshot.oauth.pending[0].state = 'connected'; window.snapshot.oauth.pending[0].account_id = 'outlook-a'; window.snapshot.accounts.push({id: 'outlook-a', kind: 'outlook', name: 'Work Outlook', email: 'work@example.test', connection_state: 'connected', provider_route: 'codex-work', provider_model: 'model', poll_enabled: true, poll_seconds: 300}); });
    await page.evaluate(() => window.nexusEmail.refresh());
    assert.match(await page.locator('#emailConnectionStatus').textContent(), /Connected/);
    assert.equal(await page.locator('#emailAutoPoll').isChecked(), true);
    assert.equal(await page.locator('#emailOAuthLink-outlook').isVisible(), false);
    await page.locator('#emailAutoPoll').uncheck();
    await page.locator('#emailSaveAssistant').click();
    await page.waitForFunction(() => window.calls.some(c => c.url.endsWith('account_save')));
    assert.equal(await page.evaluate(() => window.calls.find(c => c.url.endsWith('account_save')).data.poll_enabled), false);
    await page.waitForFunction(() => !document.querySelector('#emailDisconnect').disabled);
    await page.locator('#emailDisconnect').click();
    await page.waitForFunction(() => document.querySelector('#emailConnectionStatus').textContent.includes('Disconnected'));
    assert.equal(await page.locator('#emailReconnect').isVisible(), true);
    await page.locator('#emailAutoConnect-gmail').click();
    await page.locator('#emailClientId-gmail').fill('registered-google-client');
    await page.locator('#emailGoogleClientSecret').fill('desktop-registration-value');
    await page.getByRole('button', {name: 'Save Gmail registration'}).click();
    await page.waitForFunction(() => !document.querySelector('#emailAutoConnect-gmail').disabled);
    assert.equal(await page.locator('#emailGoogleClientSecret').inputValue(), '');
    await page.locator('#emailAutoConnect-gmail').click();
    await page.waitForFunction(() => document.querySelector('#emailOAuthLink-gmail').hasAttribute('href'));
    assert.match(await page.locator('#emailOAuthLink-gmail').getAttribute('href'), /^https:\/\/accounts\.google\.com\//);
  } finally { await browser.close(); }
});

test('Model picker exposes the route catalog and retains saved selections across catalog changes', {skip: !executablePath, timeout: 45000}, async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const page = await browser.newPage();
    await page.setContent('<main id="emailView"></main>');
    await page.evaluate(() => {
      window.calls = [];
      window.snapshot = {accounts: [{id: 'a', kind: 'outlook', name: 'Work', email: 'work@example.test', connection_state: 'connected', provider_route: 'route-a', provider_model: 'saved-legacy', poll_enabled: true}], providers: [{id: 'route-a', name: 'Codex', model: 'configured-model', models: [{id: 'supported-alpha', label: 'Alpha', source: 'catalog'}, {id: 'supported-beta', label: 'Beta', source: 'catalog'}]}, {id: 'route-b', name: 'Claude', model: 'other-default', models: [{id: 'other-supported', label: 'Other supported'}]}], messages: [{id: 'm', account_id: 'a', sender: 'sender@example.test', subject: 'Model selection', body: 'Please reply'}], drafts: [], memories: [], oauth: {outlook: {configured: true}, gmail: {configured: false}, pending: []}};
      window.request = async (url, options) => { if (!options) return structuredClone(window.snapshot); const data = JSON.parse(options.body); window.calls.push({url, data}); if (url.endsWith('account_save')) { window.snapshot.accounts[0].provider_model = data.provider_model; return {account: window.snapshot.accounts[0]}; } if (url.endsWith('create_draft')) return {draft: {id: 'new-draft'}}; return {}; };
    });
    await page.addScriptTag({content: source});
    await page.evaluate(() => window.nexusEmail.refresh());
    assert.equal(await page.locator('#emailModel').inputValue(), 'saved-legacy');
    await page.locator('#emailRefreshModels').click();
    await page.waitForFunction(() => window.calls.some(c => c.url.endsWith('refresh_models')));
    await page.waitForFunction(() => !document.querySelector('#emailRefreshModels').disabled);
    assert.match(await page.locator('#emailNotice').textContent(), /Refreshing model catalogs/);
    assert.deepEqual(await page.locator('#emailModel option').evaluateAll(items => items.map(item => item.value)), ['supported-alpha', 'supported-beta', 'configured-model', 'saved-legacy']);
    await page.locator('#emailModel').selectOption('supported-beta');
    await page.locator('#emailSaveAssistant').click();
    await page.waitForFunction(() => window.calls.some(c => c.url.endsWith('account_save')));
    assert.equal(await page.evaluate(() => window.calls.find(c => c.url.endsWith('account_save')).data.provider_model), 'supported-beta');
    await page.waitForFunction(() => !document.querySelector('#emailGenerate').disabled || !document.querySelector('#emailSaveAssistant').disabled);
    await page.locator('.email-message').click();
    await page.locator('#emailGenerate').click();
    await page.waitForFunction(() => window.calls.some(c => c.url.endsWith('create_draft')));
    assert.equal(await page.evaluate(() => window.calls.find(c => c.url.endsWith('create_draft')).data.provider_model), 'supported-beta');
    await page.evaluate(() => { window.snapshot.providers[0].models = [{id: 'brand-new-model', label: 'New catalog model'}]; });
    await page.evaluate(() => window.nexusEmail.refresh());
    assert.equal(await page.locator('#emailModel').inputValue(), 'supported-beta');
    assert.equal(await page.locator('#emailModel option[value="supported-beta"]').count(), 1);
    await page.locator('#emailProvider').selectOption('route-b');
    assert.equal(await page.locator('#emailModel').inputValue(), 'other-default');
    assert.equal(await page.locator('#emailModel option[value="other-supported"]').count(), 1);
    await page.locator('#emailProvider').selectOption('route-a');
    assert.equal(await page.locator('#emailModel').inputValue(), 'supported-beta');
  } finally { await browser.close(); }
});

test('Automatic connect waits for the first snapshot and enables immediately with the loaded route', {skip: !executablePath, timeout: 45000}, async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const page = await browser.newPage();
    await page.setContent('<main id="emailView"></main>');
    await page.evaluate(() => {
      window.request = async () => new Promise(resolve => { window.releaseSnapshot = () => resolve({accounts: [], providers: [{id: 'ready-route', name: 'Codex', model: 'ready-model'}], messages: [], drafts: [], memories: [], oauth: {outlook: {configured: true}, gmail: {configured: true}, pending: []}}); });
    });
    await page.addScriptTag({content: source});
    await page.evaluate(() => { void window.nexusEmail.refresh(); });
    assert.equal(await page.locator('#emailAutoConnect-outlook').isDisabled(), true);
    assert.equal(await page.locator('#emailAutoConnect-gmail').isDisabled(), true);
    assert.equal(await page.locator('#emailNewAccount').isDisabled(), true);
    assert.equal(await page.locator('#emailBrowserMode-browser_outlook').isDisabled(), true);
    assert.equal(await page.locator('#emailBrowserMode-browser_gmail').isDisabled(), true);
    assert.equal(await page.locator('#emailRefresh').isDisabled(), false);
    assert.match(await page.locator('#emailOAuthStatus-outlook').textContent(), /Loading sign-in settings/);
    assert.doesNotMatch(await page.locator('#emailOAuthStatus-outlook').textContent(), /registration needed/);
    await page.evaluate(() => window.releaseSnapshot());
    await page.waitForFunction(() => !document.querySelector('#emailAutoConnect-outlook').disabled);
    assert.equal(await page.locator('#emailProvider').inputValue(), 'ready-route');
    assert.equal(await page.locator('#emailModel').inputValue(), 'ready-model');
    assert.equal(await page.locator('#emailAutoConnect-gmail').isDisabled(), false);
    assert.equal(await page.locator('#emailNewAccount').isDisabled(), false);
    assert.equal(await page.locator('#emailBrowserMode-browser_outlook').isDisabled(), false);
  } finally { await browser.close(); }
});

test('Local connections require verified sign-in; browser sends and classic Outlook exports reviewed replies', {skip: !executablePath, timeout: 45000}, async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const page = await browser.newPage();
    await page.setContent('<main id="emailView"></main>');
    await page.evaluate(() => {
      window.calls = []; window.localReady = false;
      window.snapshot = {accounts: [], providers: [{id: 'route', model: 'model'}], messages: [], drafts: [], memories: []};
      window.request = async (url, options) => {
        if (!options) return structuredClone(window.snapshot);
        const data = JSON.parse(options.body); window.calls.push({url, data});
        if (url.endsWith('local_open')) return {connection: {id: 'browser-id', provider: data.provider, state: 'pending'}};
        if (url.endsWith('local_status')) return {connection: {id: 'browser-id', provider: data.kind, state: window.localReady ? 'connected' : 'pending'}};
        if (url.endsWith('local_discover')) { if (window.failDiscovery) throw new Error('Classic Outlook is not installed.'); return {connections: [{id: 'classic-id', provider: 'classic_outlook', email: 'work@example.test', state: 'connected'}]}; }
        if (url.endsWith('local_connect')) { const account = {id: data.connection_id, kind: data.kind, name: 'Local mailbox', provider_route: data.provider_route, provider_model: data.provider_model, connection_state: 'connected', poll_enabled: data.poll_enabled}; window.snapshot.accounts.push(account); return {account}; }
        if (url.endsWith('local_disconnect')) { window.snapshot.accounts.find(a => a.id === data.account_id).connection_state = 'disconnected'; return {}; }
        return {};
      };
    });
    await page.addScriptTag({content: source}); await page.evaluate(() => window.nexusEmail.refresh());
    assert.equal(await page.locator('#emailConnectionMethod').inputValue(), 'browser');
    assert.equal(await page.locator('#emailClientId-outlook').isVisible(), false);
    assert.equal(await page.locator('#emailLocalOpen-browser_gmail').isVisible(), true);
    assert.match(await page.locator('.email-connect').textContent(), /Keep Nexus running/);
    await page.locator('#emailLocalOpen-browser_outlook').click();
    await page.waitForFunction(() => !document.querySelector('#emailLocalFinish-browser_outlook').disabled);
    await page.locator('#emailLocalFinish-browser_outlook').click();
    await page.waitForFunction(() => !document.querySelector('#emailLocalFinish-browser_outlook').disabled);
    assert.equal(await page.evaluate(() => window.calls.filter(c => c.url.endsWith('local_connect')).length), 0);
    assert.doesNotMatch(await page.locator('#emailConnectionStatus').textContent(), /Connected/);
    await page.evaluate(() => { window.localReady = true; });
    await page.locator('#emailLocalFinish-browser_outlook').click();
    await page.waitForFunction(() => document.querySelector('#emailConnectionStatus').textContent.includes('Connected'));
    const connected = await page.evaluate(() => window.calls.find(c => c.url.endsWith('local_connect')).data);
    assert.deepEqual(connected, {connection_id: 'browser-id', kind: 'browser_outlook', provider_route: 'route', provider_model: 'model', poll_enabled: true, poll_seconds: 60});
    assert.equal(await page.locator('#emailSync').isDisabled(), false);
    assert.equal(await page.locator('#emailAutoPoll').isChecked(), true);
    assert.match(await page.locator('#emailApprove').textContent(), /send/);
    await page.locator('#emailDisconnect').click();
    await page.waitForFunction(() => document.querySelector('#emailConnectionStatus').textContent.includes('Disconnected'));
    await page.locator('#emailConnectionMethod').selectOption('classic');
    await page.evaluate(() => { window.failDiscovery = true; });
    await page.locator('#emailLocalOpen-classic_outlook').click();
    await page.waitForFunction(() => document.querySelector('#emailNotice').textContent.includes('not installed'));
    await page.evaluate(() => { window.failDiscovery = false; });
    await page.locator('#emailLocalOpen-classic_outlook').click();
    await page.getByRole('button', {name: 'Connect work@example.test', exact: true}).click();
    await page.waitForFunction(() => document.querySelector('#emailAccount').value === 'classic-id');
    assert.equal(await page.locator('#emailAccountForm').isVisible(), false);
    assert.equal(await page.locator('#emailOAuthAssistantSettings').isVisible(), true);
    assert.match(await page.locator('#emailApprove').textContent(), /export/);
  } finally { await browser.close(); }
});

test('Browser mode persists without recreating mailbox, sign-in stays visible, and exact reviewed reply controls sending', {skip: !executablePath, timeout: 45000}, async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    async function open(snapshot) {
      const page = await browser.newPage();
      await page.setContent('<main id="emailView"></main>');
      await page.evaluate(snapshot => {
        window.snapshot = snapshot; window.calls = [];
        window.request = async (url, options) => {
          if (!options) return structuredClone(window.snapshot);
          const data = JSON.parse(options.body); window.calls.push({url, data});
          if (url.endsWith('local_mode')) { const connection = window.snapshot.local_connections.find(item => item.id === data.connection_id); connection.browser_mode = data.browser_mode; return {connection}; }
          if (url.endsWith('local_open')) { const connection = window.snapshot.local_connections.find(item => item.id === data.connection_id) || {id: 'gmail-new', provider: data.provider}; Object.assign(connection, {browser_mode: data.browser_mode, actual_mode: 'headed', state: 'pending'}); return {connection}; }
          if (url.endsWith('approve_draft')) { Object.assign(window.snapshot.drafts[0], {edited: data.text, status: 'delivery_unknown'}); return {}; }
          return {};
        };
      }, snapshot);
      await page.addScriptTag({content: source}); await page.evaluate(() => window.nexusEmail.refresh());
      return page;
    }
    let page = await open({accounts: [{id: 'a', kind: 'browser_outlook', name: 'Mailbox', connector_id: 'c', connection_state: 'connected', provider_route: 'route'}], providers: [{id: 'route', model: 'model'}], local_connections: [{id: 'c', provider: 'browser_outlook', state: 'connected', browser_mode: 'headed'}], messages: [{id: 'm', account_id: 'a', sender: 'sender@example.test', reply_to: 'reply@example.test', subject: 'Question', body: 'Help'}], drafts: [{id: 'd', account_id: 'a', message_id: 'm', status: 'review', revision: 1, original: 'AI proposal', edited: 'AI proposal'}], memories: []});
    assert.equal(await page.locator('#emailBrowserMode-browser_outlook').inputValue(), 'headed');
    await page.locator('#emailBrowserMode-browser_outlook').selectOption('headless');
    await page.locator('#emailBrowserModeSave-browser_outlook').click();
    await page.waitForFunction(() => document.querySelector('#emailNotice').textContent.includes('mode saved'));
    assert.deepEqual(await page.evaluate(() => window.calls[0]), {url: '/api/email/local_mode', data: {kind: 'browser_outlook', connection_id: 'c', browser_mode: 'headless'}});
    assert.equal(await page.evaluate(() => window.calls.some(call => call.url.endsWith('local_connect'))), false);
    const saved = await page.evaluate(() => structuredClone(window.snapshot));
    await page.close(); page = await open(saved);
    assert.equal(await page.locator('#emailBrowserMode-browser_outlook').inputValue(), 'headless');
    assert.match(await page.locator('#emailBrowserModeStatus-browser_outlook').textContent(), /Sign-in always opens a visible browser/);
    await page.locator('#emailReconnect').click();
    await page.waitForFunction(() => window.calls.some(call => call.url.endsWith('local_open')));
    const reconnect = await page.evaluate(() => window.calls.find(call => call.url.endsWith('local_open')).data);
    assert.equal(reconnect.browser_mode, 'headless');
    assert.equal(reconnect.connection_id, 'c');
    await page.locator('.email-message').click();
    assert.match(await page.locator('#emailReplyRecipient').textContent(), /Reply to: reply@example.test/);
    assert.equal(await page.locator('#emailApprove').textContent(), 'Review & send reply');
    await page.locator('#emailReply').fill('My exact reviewed wording.');
    await page.locator('#emailApprove').click();
    await page.locator('#emailComposeSend').click();
    await page.waitForFunction(() => document.querySelector('#emailDraftStatus').textContent.includes('uncertain'));
    const approval = await page.evaluate(() => window.calls.find(call => call.url.endsWith('approve_draft')).data);
    assert.equal(approval.text, 'My exact reviewed wording.'); assert.equal(approval.revision, 1);
    assert.equal(approval.approval_contract, 'browser-send/v1');
    assert.equal(await page.locator('#emailCheckDelivery').isVisible(), false);
    assert.equal(await page.locator('#emailApprove').isDisabled(), true);
    assert.match(await page.locator('#emailDelivery').textContent(), /Check the Sent folder.*will not automatically resend/);
    await page.evaluate(() => { window.snapshot.drafts[0].status = 'sent'; window.snapshot.drafts[0].delivery_status = 'browser_confirmed'; });
    await page.evaluate(() => window.nexusEmail.refresh());
    assert.match(await page.locator('#emailDraftStatus').textContent(), /Mailbox UI accepted.*recipient delivery is not confirmed/);
    await page.evaluate(() => { window.snapshot.local_connections[0].state = 'sign_in_required'; });
    await page.evaluate(() => window.nexusEmail.refresh());
    assert.equal(await page.locator('#emailBrowserMode-browser_outlook').inputValue(), 'headless');
    assert.match(await page.locator('#emailBrowserModeStatus-browser_outlook').textContent(), /verify or reconnect.*visible browser.*preference is kept/);
    await page.locator('#emailBrowserMode-browser_gmail').selectOption('headless');
    await page.locator('#emailLocalOpen-browser_gmail').click();
    await page.waitForFunction(() => window.calls.some(call => call.url.endsWith('local_open') && call.data.provider === 'browser_gmail'));
    assert.equal(await page.evaluate(() => window.calls.find(call => call.url.endsWith('local_open') && call.data.provider === 'browser_gmail').data.browser_mode), 'headless');
    await page.locator('#emailConnectionMethod').selectOption('api');
    assert.equal(await page.locator('#emailBrowserMode-browser_outlook').isVisible(), false);
    assert.equal(await page.locator('#emailAutoConnect-outlook').isVisible(), true);
  } finally { await browser.close(); }
});

test('AI revision uses unsaved text and revision, preserves it while working and on failure, then renders a completed result', {skip: !executablePath, timeout: 45000}, async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const page = await browser.newPage(); await page.setContent('<main id="emailView"></main>');
    await page.evaluate(() => {
      window.calls = [];
      window.snapshot = {accounts: [{id: 'a', kind: 'import', provider_route: 'old-route'}], providers: [{id: 'old-route', model: 'old-model'}, {id: 'current-route', model: 'current-model'}], messages: [{id: 'm', account_id: 'a', sender: 'a@example.test', subject: 'Hi', body: 'Hello'}], drafts: [{id: 'd', account_id: 'a', message_id: 'm', status: 'review', revision: 1, original: 'Original', edited: 'Original'}], memories: [], operations: []};
      window.request = async (url, options) => {
        if (!options) return structuredClone(window.snapshot);
        const data = JSON.parse(options.body); window.calls.push({url, data});
        if (url.endsWith('revise_draft')) { window.snapshot.drafts[0].edited = data.text; window.snapshot.drafts[0].revision++; window.snapshot.operations = [{id: 'revise:d', state: 'running'}]; return {started: true}; }
        return {};
      };
    });
    await page.addScriptTag({content: source}); await page.evaluate(() => window.nexusEmail.refresh());
    await page.locator('.email-message').click(); await page.locator('#emailReply').fill('My unsaved edit');
    await page.locator('#emailProvider').selectOption('current-route');
    await page.locator('#emailRevisionRequest').fill('Make it warmer'); await page.locator('#emailRevise').click();
    await page.waitForFunction(() => window.calls.length === 1);
    assert.deepEqual(await page.evaluate(() => window.calls[0].data), {account_id: 'a', draft_id: 'd', revision: 1, text: 'My unsaved edit', instruction: 'Make it warmer', provider_route: 'current-route', provider_model: 'current-model'});
    await page.evaluate(() => window.nexusEmail.refresh());
    assert.equal(await page.locator('#emailReply').inputValue(), 'My unsaved edit');
    assert.equal(await page.locator('#emailRevise').isDisabled(), true);
    await page.evaluate(() => { window.snapshot.operations[0] = {id: 'revise:d', state: 'failed', error: 'Provider offline'}; }); await page.evaluate(() => window.nexusEmail.refresh());
    assert.equal(await page.locator('#emailReply').inputValue(), 'My unsaved edit');
    assert.match(await page.locator('#emailNotice').textContent(), /Provider offline/);
    // A failed AI call retains the durable input revision so retry needs no manual recovery.
    await page.locator('#emailRevise').click();
    await page.waitForFunction(() => window.calls.length === 2);
    assert.equal(await page.evaluate(() => window.calls[1].data.revision), 2);
    await page.evaluate(() => { window.snapshot.drafts[0].revision++; window.snapshot.drafts[0].edited = 'A warm revised reply'; window.snapshot.operations[0].state = 'completed'; }); await page.evaluate(() => window.nexusEmail.refresh());
    assert.equal(await page.locator('#emailReply').inputValue(), 'A warm revised reply');
    assert.equal(await page.locator('#emailRevisionRequest').inputValue(), '');
    assert.equal(await page.locator('#emailApprove').isDisabled(), false);
  } finally { await browser.close(); }
});

test('Restart restores pending browser sign-in and reconnect reuses the selected mailbox session', {skip: !executablePath, timeout: 45000}, async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const page = await browser.newPage(); await page.setContent('<main id="emailView"></main>');
    await page.evaluate(() => {
      window.calls = [];
      window.snapshot = {accounts: [{id: 'a', kind: 'browser_outlook', name: 'Saved browser mail', connector_id: 'saved-account-session', connection_state: 'reconnect_required', provider_route: 'route'}], providers: [{id: 'route', model: 'model'}], local_connections: [{id: 'old-pending', provider: 'browser_outlook', state: 'pending'}, {id: 'latest-pending', provider: 'browser_outlook', state: 'pending'}, {id: 'wrong-provider', provider: 'unrecognized', state: 'pending'}], messages: [], drafts: [], memories: []};
      window.request = async (url, options) => {
        if (!options) return structuredClone(window.snapshot);
        const data = JSON.parse(options.body); window.calls.push({url, data});
        return {connection: {id: data.connection_id, provider: data.provider || data.kind, state: 'pending'}};
      };
    });
    await page.addScriptTag({content: source}); await page.evaluate(() => window.nexusEmail.refresh());
    assert.equal(await page.locator('#emailLocalFinish-browser_outlook').isVisible(), true);
    await page.locator('#emailLocalFinish-browser_outlook').click();
    await page.waitForFunction(() => !document.querySelector('#emailReconnect').disabled);
    assert.equal(await page.evaluate(() => window.calls[0].data.connection_id), 'latest-pending');
    await page.locator('#emailLocalOpen-browser_outlook').click();
    await page.waitForFunction(() => !document.querySelector('#emailReconnect').disabled);
    assert.equal(await page.evaluate(() => window.calls[1].data.connection_id), 'latest-pending');
    await page.locator('#emailReconnect').click();
    await page.waitForFunction(() => !document.querySelector('#emailReconnect').disabled);
    const reconnect = await page.evaluate(() => window.calls[2]);
    assert.equal(reconnect.url, '/api/email/local_open');
    assert.equal(reconnect.data.connection_id, 'saved-account-session');
    await page.evaluate(() => window.nexusEmail.refresh());
    await page.locator('#emailLocalFinish-browser_outlook').click();
    await page.waitForFunction(() => window.calls.length === 4);
    assert.equal(await page.evaluate(() => window.calls[3].data.connection_id), 'saved-account-session');
    assert.equal(await page.evaluate(() => window.calls.some(c => c.url.includes('oauth'))), false);
  } finally { await browser.close(); }
});

test('Changed mailbox fingerprint hides obsolete mail and clears its selection without deleting saved records', {skip: !executablePath, timeout: 45000}, async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const page = await browser.newPage(); await page.setContent('<main id="emailView"></main>');
    await page.evaluate(() => {
      window.snapshot = {accounts: [{id: 'a', kind: 'browser_outlook', fingerprint: 'old-contract'}], providers: [], messages: [{id: 'old', account_id: 'a', account_fingerprint: 'old-contract', sender: 'sender@example.test', subject: 'Obsolete parsed subject', body: 'Old'}, {id: 'new', account_id: 'a', account_fingerprint: 'new-contract', sender: 'sender@example.test', subject: 'Valid subject', body: 'New'}, {id: 'legacy', account_id: 'a', sender: 'sender@example.test', subject: 'Legacy fixture', body: 'Legacy'}], drafts: [{id: 'd', account_id: 'a', message_id: 'old', status: 'review', revision: 1, original: 'Old reply', edited: 'Old reply'}], memories: []};
      window.request = async () => structuredClone(window.snapshot);
    });
    await page.addScriptTag({content: source}); await page.evaluate(() => window.nexusEmail.refresh());
    await page.locator('.email-message').filter({hasText: 'Obsolete parsed subject'}).click();
    assert.equal(await page.locator('#emailReply').inputValue(), 'Old reply');
    await page.evaluate(() => { window.snapshot.accounts[0].fingerprint = 'new-contract'; }); await page.evaluate(() => window.nexusEmail.refresh());
    assert.equal(await page.locator('.email-message').count(), 2);
    assert.equal(await page.locator('.email-message').filter({hasText: 'Obsolete parsed subject'}).count(), 0);
    assert.equal(await page.locator('#emailReply').inputValue(), '');
    assert.equal(await page.locator('#emailApprove').isDisabled(), true);
    assert.equal(await page.locator('#emailGenerate').isDisabled(), true);
    assert.equal(await page.evaluate(() => window.snapshot.messages.length), 3);
    assert.equal(await page.evaluate(() => window.snapshot.drafts.length), 1);
    await page.locator('.email-message').filter({hasText: 'Valid subject'}).click();
    assert.match(await page.locator('#emailIncoming').textContent(), /Valid subject/);
    assert.equal(await page.locator('#emailGenerate').isDisabled(), false);
  } finally { await browser.close(); }
});

test('Automatic learning tabs isolate recipients and mailboxes, preserve edits, and support saving and deletion', {skip: !executablePath, timeout: 45000}, async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const page = await browser.newPage(); await page.setContent('<main id="emailView"></main>');
    await page.evaluate(() => {
      window.calls = [];
      window.snapshot = {accounts:[{id:'a', name:'Primary', kind:'import'}, {id:'b', name:'Other', kind:'import'}], providers:[], messages:[], drafts:[], memories:[{id:'manual',account_id:'a',text:'Keep replies concise'}], automatic_memories:[
        {id:'auto1',account_id:'a',recipient:'customer@example.test',text:'Use a professional greeting',revision:1,source_draft_id:'d1',source_revision:2},
        {id:'auto2',account_id:'a',recipient:'grandma@example.test',text:'Use a warm family greeting',revision:1},
        {id:'auto3',account_id:'a',recipient:'grandma@example.test',text:'Sign off with love',revision:1},
        {id:'auto1',account_id:'b',recipient:'other@example.test',text:'Other mailbox rule',revision:1}
      ]};
      window.request = async (url, options) => {
        if (!options) return structuredClone(window.snapshot);
        const data = JSON.parse(options.body); window.calls.push({url,...data});
        if (url.endsWith('/memory_delete')) window.snapshot.automatic_memories = window.snapshot.automatic_memories.filter(m => m.id !== data.memory_id || m.account_id !== data.account_id);
        if (url.endsWith('/memory_save')) window.snapshot.automatic_memories.find(m => m.id === data.memory_id && m.account_id === data.account_id).text = data.text;
        return {};
      };
    });
    await page.addScriptTag({content:source}); await page.evaluate(() => window.nexusEmail.refresh());
    await page.locator('#emailAccount').selectOption('a');
    assert.equal(await page.locator('#emailMemories textarea').inputValue(), 'Keep replies concise');
    assert.ok(await page.locator('#emailManualLearning').isVisible());
    await page.locator('#emailManualLearningTab').focus(); await page.keyboard.press('ArrowRight');
    assert.equal(await page.locator('#emailAutomaticLearningTab').getAttribute('aria-selected'), 'true');
    assert.equal(await page.locator('#emailAutomaticLearningTab').textContent(), 'What your assistant has learned AUTOMATICALLY');
    assert.equal(await page.locator('.email-recipient-memory').count(), 2);
    assert.equal(await page.locator('.email-recipient-memory').nth(1).locator('textarea').count(), 2);
    assert.ok(!(await page.locator('#emailManualLearning').isVisible()));
    const customer = page.locator('#email-memory-automatic-auto1'); await customer.fill('Edited customer greeting');
    await page.evaluate(() => window.nexusEmail.refresh()); assert.equal(await customer.inputValue(), 'Edited customer greeting');
    await page.locator('#emailAccount').selectOption('b');
    assert.equal(await customer.inputValue(), 'Other mailbox rule');
    assert.equal(await page.locator('.email-recipient-memory h3').textContent(), 'other@example.test');
    await page.locator('#emailAccount').selectOption('a'); assert.equal(await customer.inputValue(), 'Edited customer greeting');
    await customer.locator('..').getByRole('button', {name:'Save',exact:true}).click();
    await page.waitForFunction(() => window.calls.length === 1 && !document.querySelector('#emailNotice').textContent.includes('Working'));
    assert.deepEqual(await page.evaluate(() => window.calls[0]), {url:'/api/email/memory_save',account_id:'a',memory_id:'auto1',text:'Edited customer greeting'});
    await customer.locator('..').getByRole('button', {name:'Delete',exact:true}).click();
    await page.waitForFunction(() => !document.querySelector('#email-memory-automatic-auto1'));
    assert.equal(await page.locator('.email-recipient-memory').count(), 1);
    await page.evaluate(() => {window.snapshot.automatic_memories = [];});
    await page.locator('#emailAutomaticLearningTab').focus(); await page.evaluate(() => window.nexusEmail.refresh());
    assert.match(await page.locator('#emailAutomaticMemories').textContent(), /No automatic preferences yet/);
    await page.keyboard.press('Home'); assert.equal(await page.locator('#emailManualLearningTab').getAttribute('aria-selected'), 'true');
    assert.equal(await page.locator('#emailMemories textarea').inputValue(), 'Keep replies concise');
    await page.locator('#emailMemoryText').fill('Unsubmitted manual preference');
    await page.locator('#emailAccount').selectOption('b'); assert.equal(await page.locator('#emailMemoryText').inputValue(), '');
    await page.locator('#emailAccount').selectOption('a'); assert.equal(await page.locator('#emailMemoryText').inputValue(), 'Unsubmitted manual preference');
  } finally { await browser.close(); }
});

test('Automatic revision learning reports success, one-off, missing recipient, and extraction failure separately', {skip: !executablePath, timeout:45000}, async () => {
  const browser = await chromium.launch({executablePath,headless:true});
  try {
    const page = await browser.newPage(); await page.setContent('<main id="emailView"></main>');
    await page.evaluate(() => {
      window.snapshot = {accounts:[{id:'a',kind:'import'}],providers:[],memories:[],messages:[{id:'m',account_id:'a',sender:'friend@example.test',subject:'Hello'}],drafts:[{id:'d',account_id:'a',message_id:'m',status:'review',original:'Hi',edited:'Hello',revision:2,automatic_learning_status:'learned'}]};
      window.request = async () => structuredClone(window.snapshot);
    });
    await page.addScriptTag({content:source}); await page.evaluate(() => window.nexusEmail.refresh());
    await page.locator('.email-message').click();
    for (const [status, expected] of [['learned',/saved for this recipient/],['no_reusable_preferences',/No reusable preference/],['no_recipient',/no single recipient/],['failed',/reply was revised, but automatic learning failed/]]) {
      await page.evaluate(status => {window.snapshot.drafts[0].automatic_learning_status = status;},status);
      await page.evaluate(() => window.nexusEmail.refresh());
      assert.match(await page.locator('#emailAutomaticLearningStatus').textContent(), expected);
      assert.equal(await page.locator('#emailReply').inputValue(),'Hello');
    }
    assert.match(await page.locator('#emailLearn').locator('..').textContent(), /controls learning from edits when you approve/);
  } finally { await browser.close(); }
});

test('Automatic learning outcomes explain empty results per recipient and retry saved requests without changing or sending replies', {skip: !executablePath, timeout:45000}, async () => {
  const browser = await chromium.launch({executablePath,headless:true});
  try {
    const page = await browser.newPage(); await page.setContent('<main id="emailView"></main>');
    await page.evaluate(() => {
      window.calls = [];
      window.snapshot = {accounts:[{id:'a',name:'Primary',kind:'import'},{id:'b',name:'Other',kind:'import'}],providers:[],memories:[],messages:[],drafts:[],automatic_memories:[],automatic_learning_outcomes:[]};
      window.request = async (url, options) => {
        if (!options) return structuredClone(window.snapshot);
        const data = JSON.parse(options.body); window.calls.push({url,...data});
        if (window.failRetry) throw new Error('Provider unavailable. Retry later.');
        window.snapshot.operations = [{id:'automatic-learning:d',state:'running'}];
        return {started:true};
      };
    });
    await page.addScriptTag({content:source}); await page.evaluate(() => window.nexusEmail.refresh());
    await page.locator('#emailAccount').selectOption('a'); await page.locator('#emailAutomaticLearningTab').click();
    assert.match(await page.locator('#emailAutomaticMemories').textContent(), /No automatic preferences yet/);
    await page.evaluate(() => {
      window.snapshot.automatic_learning_outcomes = [
        {account_id:'a',recipient:'grandma@example.test',draft_id:'d',revision:4,source_revision:3,request_index:0,request_fingerprint:'request0',requested_change:'Correct my spelling',status:'not_recorded',retry_available:true},
        {account_id:'a',recipient:'grandma@example.test',draft_id:'d',revision:4,source_revision:4,request_index:1,request_fingerprint:'request1',requested_change:'Use a warmer tone',status:'no_reusable_preferences',retry_available:true},
        {account_id:'a',recipient:'customer@example.test',draft_id:'customer',revision:2,request_index:0,request_fingerprint:'customer1',status:'failed',retry_available:true},
        {account_id:'b',recipient:'private@example.test',draft_id:'other',revision:2,request_index:0,status:'learned',retry_available:false}
      ];
      window.snapshot.automatic_memories = [{id:'c',account_id:'a',recipient:'customer@example.test',text:'Use a professional greeting'}];
    });
    await page.evaluate(() => window.nexusEmail.refresh());
    assert.equal(await page.locator('.email-recipient-memory').count(),2);
    assert.equal(await page.locator('.email-learning-attempt').count(),3);
    const grandma = page.locator('.email-recipient-memory').filter({hasText:'grandma@example.test'});
    assert.match(await grandma.textContent(), /earlier revision has no recorded learning outcome/);
    assert.match(await grandma.textContent(), /AI checked your revision but found no reusable preference/);
    assert.doesNotMatch(await page.locator('#emailAutomaticMemories').textContent(), /Use Revise with AI on a reply to start|private@example/);
    assert.equal(await page.locator('#email-memory-automatic-c').inputValue(),'Use a professional greeting');
    await grandma.getByRole('button',{name:'Retry learning'}).nth(1).click();
    await page.waitForFunction(() => window.calls.length === 1);
    assert.deepEqual(await page.evaluate(() => window.calls[0]), {url:'/api/email/retry_automatic_learning',account_id:'a',draft_id:'d',revision:4,request_index:1,request_fingerprint:'request1'});
    await page.waitForFunction(() => document.querySelector('.email-learning-outcome').textContent.includes('Learning from'));
    assert.ok(await grandma.getByRole('button',{name:'Retry learning'}).nth(1).isDisabled());
    await page.evaluate(() => {window.snapshot.operations[0] = {id:'automatic-learning:d',state:'failed',error:'Learning provider timed out.'};});
    await page.evaluate(() => window.nexusEmail.refresh());
    assert.match(await grandma.textContent(), /Learning provider timed out/);
    assert.ok(await grandma.getByRole('button',{name:'Retry learning'}).nth(1).isEnabled());
    await page.evaluate(() => {window.failRetry = true;});
    await grandma.getByRole('button',{name:'Retry learning'}).nth(1).click();
    await page.waitForFunction(() => document.querySelector('#emailNotice').textContent.includes('Provider unavailable'));
    assert.match(await grandma.textContent(), /Provider unavailable/);
    await page.locator('#emailAccount').selectOption('b');
    assert.equal(await page.locator('.email-recipient-memory h3').textContent(),'private@example.test');
    assert.doesNotMatch(await page.locator('#emailAutomaticMemories').textContent(),/grandma|customer|Provider unavailable/);
    await page.locator('#emailAccount').selectOption('a');
    await page.evaluate(() => {
      window.snapshot.operations[0] = {id:'automatic-learning:d',state:'completed'};
      window.snapshot.automatic_learning_outcomes[1].status = 'learned';
      window.snapshot.automatic_memories.push({id:'g',account_id:'a',recipient:'grandma@example.test',text:'Use a warm tone'});
      window.failRetry = false;
    });
    await grandma.getByRole('button',{name:'Retry learning'}).nth(1).click();
    await page.waitForFunction(() => window.calls.length === 3);
    await page.evaluate(() => {window.snapshot.operations[0].state = 'completed';}); await page.evaluate(() => window.nexusEmail.refresh());
    assert.match(await grandma.textContent(), /Reusable preferences were learned/);
    assert.equal(await page.locator('#email-memory-automatic-g').inputValue(),'Use a warm tone');
    assert.ok((await page.evaluate(() => window.calls)).every(call => call.url.endsWith('/retry_automatic_learning')));
  } finally {await browser.close();}
});

test('Every control explains itself on hover and the AI session is repaired only after a confirmed diagnosis', {skip: !executablePath, timeout: 45000}, async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const page = await browser.newPage(); await page.setContent('<main id="emailView"></main>');
    await page.evaluate(() => {
      window.calls = [];
      window.snapshot = {accounts: [{id: 'a', kind: 'browser_outlook', provider_route: 'assistant'}], providers: [{id: 'assistant', model: 'model'}], messages: [{id: 'm', account_id: 'a', sender: 'writer@example.test', subject: 'Hello', body: 'Hello'}], drafts: [], memories: []};
      window.request = async (url, options) => {
        if (!options) return structuredClone(window.snapshot);
        const body = JSON.parse(options.body); window.calls.push({url, body});
        if (url === '/api/team/repair-plan') return window.plan;
        if (url === '/api/team/repair-claude') return {opened: true, note: 'Claude repair opened in its own terminal.'};
        if (url === '/api/team/login') return {opened: true, note: 'Claude sign-in opened in its own window.'};
        return {};
      };
      window.plan = {route: 'assistant', repair: {summary: 'This route is signed out of the Claude command line.', diagnosis_fingerprint: 'print-1', actions: [{id: 'repair-claude', route: 'assistant', diagnosis_fingerprint: 'print-1'}]}};
    });
    await page.addScriptTag({content: source}); await page.evaluate(() => window.nexusEmail.refresh());
    // Buttons and pickers carry their own plain-English explanation.
    for (const id of ['emailNewAccount', 'emailRefreshModels', 'emailProviderCheck', 'emailSync', 'emailGenerate', 'emailApprove', 'emailDisconnect', 'emailProvider', 'emailAutoSeconds']) {
      assert.ok((await page.locator('#' + id).getAttribute('title') || '').length > 10, id + ' has no hover explanation');
    }
    assert.match(await page.locator('.email-message').first().getAttribute('title'), /Open this message/);
    // The repair stays hidden until a diagnosis actually offers it.
    assert.equal(await page.locator('#emailProviderRepairPanel').isHidden(), true);
    await page.locator('#emailProviderCheck').click();
    await page.waitForFunction(() => !document.querySelector('#emailProviderRepairPanel').hidden);
    assert.match(await page.locator('#emailProviderRepairStatus').textContent(), /signed out of the Claude command line/);
    // Signing the command line out is consequential, so it needs its own confirmation.
    assert.equal(await page.locator('#emailProviderRepairRun').isDisabled(), true);
    await page.locator('#emailProviderRepairConfirm').check();
    await page.locator('#emailProviderRepairRun').click();
    await page.waitForFunction(() => window.calls.some(call => call.url === '/api/team/repair-claude'));
    const repair = await page.evaluate(() => window.calls.find(call => call.url === '/api/team/repair-claude').body);
    assert.deepEqual(repair, {route: 'assistant', diagnosis_fingerprint: 'print-1'});
    await page.waitForFunction(() => document.querySelector('#emailProviderRepairPanel').hidden);
    assert.match(await page.locator('#emailProviderRepairStatus').textContent(), /opened in its own terminal/);
    // A route that is only signed out gets its ordinary sign-in window on one press.
    await page.evaluate(() => { window.plan = {route: 'assistant', repair: {summary: 'Not logged in.', actions: [{id: 'login', route: 'assistant'}, {id: 'check', route: 'assistant'}]}}; });
    await page.locator('#emailProviderCheck').click();
    await page.waitForFunction(() => window.calls.some(call => call.url === '/api/team/login'));
    assert.deepEqual(await page.evaluate(() => window.calls.find(call => call.url === '/api/team/login').body), {route: 'assistant'});
    assert.match(await page.locator('#emailProviderRepairStatus').textContent(), /Not logged in.*sign-in opened in its own window/);
    assert.equal(await page.locator('#emailProviderRepairPanel').isHidden(), true, 'a signed-out route is not offered the sign-out repair');
    // A healthy route reports that plainly and offers no sign-in or sign-out.
    await page.evaluate(() => { window.plan = {route: 'assistant', repair: {summary: 'This route answered its status check.', actions: []}}; });
    const before = await page.evaluate(() => window.calls.length);
    await page.locator('#emailProviderCheck').click();
    await page.waitForFunction(() => document.querySelector('#emailProviderRepairStatus').textContent === 'This route answered its status check.');
    assert.equal(await page.locator('#emailProviderRepairPanel').isHidden(), true);
    assert.equal(await page.evaluate(count => window.calls.slice(count).filter(call => call.url !== '/api/team/repair-plan').length, before), 0, 'a healthy route opens nothing');
  } finally { await browser.close(); }
});

test('The inbox can be searched and reordered, and a long inbox scrolls in place instead of stretching the page', {skip: !executablePath, timeout: 45000}, async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const page = await browser.newPage(); await page.setContent('<main id="emailView"></main>');
    await page.addStyleTag({path: path.join(ui, 'email.css')});
    await page.evaluate(() => {
      const messages = [];
      for (let index = 0; index < 40; index++) messages.push({id: 'bulk-' + index, account_id: 'a', sender: 'bulk' + index + '@example.test',
        subject: 'Weekly digest ' + index, body: 'Bulk body', imported_at: '2025-01-0' + (1 + index % 8) + 'T09:00:00Z'});
      messages.push({id: 'invoice', account_id: 'a', sender: 'zara@supplier.test', subject: 'Invoice overdue', body: 'Please pay', imported_at: '2025-02-01T09:00:00Z'});
      messages.push({id: 'oldest', account_id: 'a', sender: 'adam@example.test', subject: 'Ancient thread', body: 'Old', imported_at: '2024-01-01T09:00:00Z'});
      window.snapshot = {accounts: [{id: 'a', kind: 'browser_outlook'}], providers: [], drafts: [], memories: [], messages};
      window.request = async () => structuredClone(window.snapshot);
    });
    await page.addScriptTag({content: source}); await page.evaluate(() => window.nexusEmail.refresh());
    assert.equal(await page.locator('.email-message').count(), 42);
    assert.match(await page.locator('#emailQueue > p').first().textContent(), /^42 imported messages\./);

    // The list keeps its own scrollbar, so the page does not grow with the inbox.
    const scroll = page.locator('.email-queue-scroll');
    const box = await scroll.evaluate(node => ({visible: node.scrollHeight > node.clientHeight, height: node.clientHeight}));
    assert.equal(box.visible, true, 'a long inbox must overflow its own box');
    const panelFit = async () => page.evaluate(() => {
      const queue = document.querySelector('.email-queue');
      const review = document.querySelector('.email-review');
      const list = document.querySelector('.email-queue-scroll');
      const style = getComputedStyle(queue);
      return {gap: queue.getBoundingClientRect().bottom - parseFloat(style.paddingBottom) - parseFloat(style.borderBottomWidth) - list.getBoundingClientRect().bottom,
        queueHeight: queue.getBoundingClientRect().height, reviewHeight: review.getBoundingClientRect().height,
        pageHeight: document.documentElement.scrollHeight};
    });
    const initialFit = await panelFit();
    assert.ok(Math.abs(initialFit.gap) < 2, 'the list fills the parent down to its bottom padding');
    assert.equal(initialFit.queueHeight, initialFit.reviewHeight);
    await page.locator('.email-review').evaluate(node => { node.style.minHeight = '1800px'; });
    const tallFit = await panelFit();
    assert.ok(Math.abs(tallFit.gap) < 2, 'the list follows a taller review panel');
    assert.ok((await scroll.boundingBox()).height > box.height, 'available list space grows with its parent');
    await page.evaluate(() => { window.snapshot.messages.push(...Array.from({length: 150}, (_, index) => ({id: 'extra-' + index, account_id: 'a', subject: 'Extra', sender: 'more@example.test', body: 'More'}))); });
    await page.evaluate(() => window.nexusEmail.refresh());
    assert.equal((await panelFit()).pageHeight, tallFit.pageHeight, 'more messages do not stretch the page');
    await page.evaluate(() => { window.snapshot.messages = window.snapshot.messages.filter(message => !message.id.startsWith('extra-')); });
    await page.evaluate(() => window.nexusEmail.refresh());
    await page.locator('.email-review').evaluate(node => { node.style.minHeight = ''; });
    for (const width of [950, 650, 390]) {
      await page.setViewportSize({width, height: 800});
      assert.ok((await scroll.boundingBox()).height <= 460, 'stacked inbox retains a bounded scroll area');
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, 'no horizontal overflow');
    }
    await page.setViewportSize({width: 1280, height: 800});
    await scroll.evaluate(node => { node.scrollTop = 240; });
    await page.evaluate(() => window.nexusEmail.refresh());
    assert.equal(await scroll.evaluate(node => node.scrollTop), 240, 'an inbox refresh keeps the reader in place');

    // Searching narrows the list and says how much of the inbox is shown.
    await page.locator('#emailInboxSearch').fill('invoice');
    assert.equal(await page.locator('.email-message').count(), 1);
    assert.match(await page.locator('.email-message').first().textContent(), /Invoice overdue/);
    assert.match(await page.locator('#emailQueue > p').first().textContent(), /^Showing 1 of 42 imported messages\./);
    await page.locator('#emailInboxSearch').fill('supplier.test');
    assert.equal(await page.locator('.email-message').count(), 1, 'the sender is searched as well as the subject');
    await page.locator('#emailInboxSearch').fill('nothing matches this');
    assert.equal(await page.locator('.email-message').count(), 0);
    assert.match(await page.locator('#emailQueue').textContent(), /No messages match your search/);
    // A search that hides everything must not claim the mailbox is empty.
    assert.doesNotMatch(await page.locator('#emailQueue').textContent(), /No messages yet/);
    await page.locator('#emailInboxSearch').fill('');
    assert.equal(await page.locator('.email-message').count(), 42);

    // Sorting reorders the same messages.
    assert.match(await page.locator('.email-message').first().textContent(), /Invoice overdue/);
    await page.locator('#emailInboxSort').selectOption('oldest');
    assert.match(await page.locator('.email-message').first().textContent(), /Ancient thread/);
    await page.locator('#emailInboxSort').selectOption('sender');
    assert.match(await page.locator('.email-message').first().textContent(), /adam@example.test/);
    assert.match(await page.locator('.email-message').last().textContent(), /zara@supplier.test/);
    await page.locator('#emailInboxSort').selectOption('subject');
    assert.match(await page.locator('.email-message').first().textContent(), /Ancient thread/);
    assert.match(await page.locator('.email-message').last().textContent(), /Weekly digest 9/);

    // The chosen order and search survive an inbox refresh, and opening a message still works.
    await page.locator('#emailInboxSearch').fill('Invoice');
    await page.evaluate(() => window.nexusEmail.refresh());
    assert.equal(await page.locator('#emailInboxSort').inputValue(), 'subject');
    assert.equal(await page.locator('.email-message').count(), 1);
    await page.locator('.email-message').first().click();
    assert.match(await page.locator('#emailIncoming').textContent(), /Please pay/);
  } finally { await browser.close(); }
});

test('mail draft window reviews headers, cancels without approval, preserves edits and guards stale revisions', {skip: !executablePath, timeout:45000}, async()=>{
  const browser=await chromium.launch({executablePath,headless:true});
  try {
    const page=await browser.newPage();await page.setContent('<main id="emailView" class="email-workspace"></main>');
    await page.addStyleTag({content:fs.readFileSync(path.join(ui,'styles.css'),'utf8')});
    await page.addStyleTag({content:fs.readFileSync(path.join(ui,'email.css'),'utf8')});
    await page.evaluate(()=>{
      window.calls=[];
      window.snapshot={accounts:[{id:'a',kind:'browser_outlook',email:'owner@example.test'}],providers:[],messages:[{id:'m',account_id:'a',sender:'sender@example.test',reply_to:'reply@example.test',subject:'Question',body:'Original'}],drafts:[{id:'d',account_id:'a',message_id:'m',status:'review',original:'Draft',edited:'Draft',revision:1}],memories:[]};
      window.request=async(url,options)=>{if(!options)return structuredClone(window.snapshot);window.calls.push({url,data:JSON.parse(options.body)});window.snapshot.drafts[0].status='approved';return {};};
    });
    await page.addScriptTag({content:source});await page.evaluate(()=>window.nexusEmail.refresh());
    await page.locator('.email-message').click();await page.locator('#emailApprove').click();
    assert.equal(await page.locator('#emailComposeFrom').inputValue(),'owner@example.test');
    assert.equal(await page.locator('#emailComposeTo').inputValue(),'reply@example.test');
    assert.equal(await page.locator('#emailComposeSubject').inputValue(),'Re: Question');
    assert.equal(await page.locator('#emailComposeBody').inputValue(),'Draft');
    await page.locator('#emailComposeBody').fill('Keep this edit');
    await page.locator('#emailComposeCancel').click();assert.equal(await page.evaluate(()=>window.calls.length),0);
    assert.equal(await page.locator('#emailReply').inputValue(),'Keep this edit');
    await page.locator('#emailApprove').click();
    await page.evaluate(()=>{window.snapshot.drafts[0].revision=2;});await page.evaluate(()=>window.nexusEmail.refresh());
    await page.locator('#emailComposeSend').click();assert.match(await page.locator('#emailCompose [role=alert]').textContent(),/draft changed/);
    assert.equal(await page.evaluate(()=>window.calls.length),0);
    await page.locator('#emailComposeCancel').click();await page.locator('#emailApprove').click();
    await page.locator('#emailComposeBody').fill('Exact reviewed edit');
    await page.locator('#emailCompose').evaluate(e=>{e.scrollTop=0;});
    await page.screenshot({path:path.join(require('node:os').tmpdir(),'nexus-email-compose-desktop.png')});
    await page.setViewportSize({width:390,height:850});
    assert.equal(await page.locator('#emailCompose').evaluate(e=>e.scrollWidth<=e.clientWidth),true);
    await page.locator('#emailComposeSend').click();
    await page.waitForFunction(()=>!document.querySelector('#emailCompose').open);
    const calls=await page.evaluate(()=>window.calls);assert.equal(calls.length,1);assert.equal(calls[0].data.text,'Exact reviewed edit');assert.equal(calls[0].data.approval_contract,'browser-send/v1');
  }finally{await browser.close();}
});

test('one send button reopens a failed approval and explicit version choice controls the exact approved text', {skip: !executablePath, timeout:45000}, async()=>{
  const browser=await chromium.launch({executablePath,headless:true});
  try {
    const page=await browser.newPage();await page.setContent('<main id="emailView" class="email-workspace"></main>');
    await page.addStyleTag({content:fs.readFileSync(path.join(ui,'styles.css'),'utf8')});await page.addStyleTag({content:fs.readFileSync(path.join(ui,'email.css'),'utf8')});
    await page.evaluate(()=>{
      window.calls=[];
      window.snapshot={accounts:[{id:'a',kind:'browser_outlook',email:'owner@example.test'}],providers:[],messages:[{id:'m',account_id:'a',sender:'sender@example.test',subject:'Question',body:'Original incoming'}],drafts:[{id:'d',account_id:'a',message_id:'m',status:'approved',original:'Original AI version',edited:'My edited version',revision:2,error:'A unique Reply control is not available in the original message.'}],memories:[],operations:[{id:'finalize:d',state:'failed',error:'Reply unavailable'}]};
      window.request=async(url,options)=>{if(!options)return structuredClone(window.snapshot);const data=JSON.parse(options.body);window.calls.push({url,data});Object.assign(window.snapshot.drafts[0],{edited:data.text,revision:data.revision+1,status:'approved',error:'Reply temporarily unavailable'});return {draft:structuredClone(window.snapshot.drafts[0])};};
    });
    await page.addScriptTag({content:source});await page.evaluate(()=>window.nexusEmail.refresh());await page.locator('.email-message').click();
    assert.equal(await page.locator('#emailApprove').isEnabled(),true);assert.equal(await page.locator('#emailResume').isVisible(),false);
    await page.locator('#emailUseOriginal').check();assert.match(await page.locator('#emailSendVersionNotice').textContent(),/Original AI draft/);
    await page.locator('#emailApprove').click();assert.equal(await page.locator('#emailComposeBody').inputValue(),'Original AI version');
    await page.locator('#emailComposeVersion').selectOption('edited');assert.equal(await page.locator('#emailComposeBody').inputValue(),'My edited version');
    await page.locator('#emailComposeCancel').click();assert.equal(await page.evaluate(()=>window.calls.length),0);
    await page.locator('#emailUseOriginal').check();await page.locator('#emailApprove').click();await page.locator('#emailComposeSend').click();
    await page.waitForFunction(()=>!document.querySelector('#emailCompose').open);
    assert.equal(await page.evaluate(()=>window.calls[0].data.text),'Original AI version');
    assert.equal(await page.locator('#emailApprove').isEnabled(),true);
    await page.locator('#emailUseEdited').check();await page.locator('#emailReply').fill('New edited version after failure');
    await page.locator('#emailApprove').click();assert.equal(await page.locator('#emailComposeBody').inputValue(),'New edited version after failure');
    await page.locator('#emailComposeSend').click();await page.waitForFunction(()=>window.calls.length===2);
    assert.equal(await page.evaluate(()=>window.calls[1].data.text),'New edited version after failure');
    await page.waitForFunction(()=>!document.querySelector('#emailCompose').open);
    await page.evaluate(()=>{window.snapshot.drafts[0].status='delivery_unknown';});await page.evaluate(()=>window.nexusEmail.refresh());assert.equal(await page.locator('#emailApprove').isDisabled(),true);
  }finally{await browser.close();}
});

test('readiness precedes revision and compose, rebases metadata-only changes, and preserves local edits', {skip: !executablePath, timeout:45000}, async()=>{
  const browser=await chromium.launch({executablePath,headless:true});
  try {
    const page=await browser.newPage();await page.setContent('<main id="emailView"></main>');
    await page.evaluate(()=>{
      window.calls=[];window.holdPreparation=false;
      window.snapshot={accounts:[{id:'a',kind:'browser_outlook',email:'owner@example.test'}],providers:[],messages:[{id:'m',account_id:'a',sender:'sender@example.test',subject:'Question',body:'Incoming'}],drafts:[{id:'d',account_id:'a',message_id:'m',status:'approved',original:'Original',edited:'Saved version',revision:2,error:'Old send failure'}],memories:[],operations:[{id:'finalize:d',state:'failed',error:'Old send failure'}]};
      window.request=async(url,options)=>{
        if(!options)return structuredClone(window.snapshot);
        const data=JSON.parse(options.body);window.calls.push({url,data});const draft=window.snapshot.drafts[0];
        if(url.endsWith('prepare_draft')) {
          if(window.holdPreparation)await new Promise(resolve=>window.finishPreparation=resolve);
          if(window.preparationError)throw new Error(window.preparationError);
          draft.status='review';draft.error='';delete draft.approved_at;window.snapshot.operations=[];
          return {ready:true,draft:structuredClone(draft)};
        }
        if(url.endsWith('revise_draft')) {draft.edited='AI revised local text';draft.revision=data.revision+2;return {started:true};}
        if(url.endsWith('approve_draft')) {draft.status='sent';return {draft:structuredClone(draft)};}
        throw new Error('Unexpected action '+url);
      };
    });
    await page.addScriptTag({content:productionSource});await page.evaluate(()=>window.nexusEmail.refresh());await page.locator('.email-message').click();
    await page.locator('#emailReply').fill('My local unsaved text');await page.locator('#emailRevisionRequest').fill('Make it shorter');
    await page.evaluate(()=>{window.snapshot.drafts[0].revision=3;});
    await page.locator('#emailRevise').click();await page.waitForFunction(()=>window.calls.length===2);
    assert.deepEqual(await page.evaluate(()=>window.calls.map(c=>c.url.split('/').pop())),['prepare_draft','revise_draft']);
    assert.equal(await page.evaluate(()=>window.calls[1].data.revision),3);assert.equal(await page.evaluate(()=>window.calls[1].data.text),'My local unsaved text');
    await page.waitForFunction(()=>document.querySelector('#emailReply').value==='AI revised local text');
    await page.evaluate(()=>{window.holdPreparation=true;});await page.locator('#emailApprove').click();
    await page.waitForFunction(()=>!!window.finishPreparation);
    assert.equal(await page.locator('#emailCompose').isVisible(),false);assert.equal(await page.locator('#emailApprove').isDisabled(),true);
    assert.match(await page.locator('#emailSendStatus').textContent(),/Checking mailbox/);
    await page.evaluate(()=>window.finishPreparation());await page.locator('#emailCompose').waitFor({state:'visible'});
    assert.equal(await page.locator('#emailComposeBody').inputValue(),'AI revised local text');
    assert.equal(await page.evaluate(()=>window.calls.some(c=>c.url.endsWith('approve_draft'))),false);
    await page.locator('#emailComposeCancel').click();
    await page.evaluate(()=>{window.holdPreparation=false;window.preparationError='Mailbox still loading. Try again.';});
    await page.locator('#emailApprove').click();await page.waitForFunction(()=>document.querySelector('#emailSendStatus').textContent.includes('Mailbox still loading'));
    assert.equal(await page.locator('#emailCompose').isVisible(),false);assert.equal(await page.locator('#emailApprove').isEnabled(),true);
    assert.equal(await page.locator('#emailReply').inputValue(),'AI revised local text');
    await page.evaluate(()=>{window.preparationError='';});await page.locator('#emailApprove').click();await page.locator('#emailCompose').waitFor({state:'visible'});
    await page.locator('#emailComposeSend').click();await page.waitForFunction(()=>window.calls.some(c=>c.url.endsWith('approve_draft')));
  }finally{await browser.close();}
});
