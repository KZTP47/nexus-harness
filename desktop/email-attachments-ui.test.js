// Mail tab pictures and files: shown with the email, attached to a reply, and
// given to the AI with a change request. Offline page with a scripted server.
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
const source = fs.readFileSync(path.join(ui, 'email.js'), 'utf8');
// A real 1 x 1 PNG, so the preview decodes.
const PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYPj/HwADAgH/p+FUpQAAAABJRU5ErkJggg==';

test('Pictures show with the email, files attach to the reply, and request files reach Revise with AI', {skip: !executablePath, timeout: 60000}, async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const page = await browser.newPage(); await page.setContent('<main id="emailView"></main>');
    await page.evaluate(png => {
      window.calls = [];
      const picture = {id: 'pic', name: 'chart.png', type: 'image/png', size: 68, sha256: 'a'.repeat(64), image: true, inline: true};
      const invoice = {id: 'pdf', name: 'invoice.pdf', type: 'application/pdf', size: 2048, sha256: 'b'.repeat(64), image: false, inline: false};
      window.snapshot = {accounts: [{id: 'a', kind: 'browser_outlook', email: 'me@example.test', provider_route: 'assistant'}], providers: [{id: 'assistant', model: 'model'}], memories: [],
        messages: [{id: 'm', account_id: 'a', sender: 'colleague@example.test', subject: 'Numbers', body: 'See the chart.', attachments: [picture, invoice, {id: 'gone', name: 'huge.zip', note: 'Larger than the attachment size limit; open it in the mailbox.'}]}],
        drafts: [{id: 'd', account_id: 'a', message_id: 'm', status: 'review', original: 'Thanks.', edited: 'Thanks.', revision: 2, attachments: []}]};
      window.request = async (url, options) => {
        if (!options) return structuredClone(window.snapshot);
        const action = url.split('/').pop(); const data = JSON.parse(options.body); window.calls.push({action, data});
        const draft = window.snapshot.drafts[0];
        if (action === 'attachment_content') return {attachment: {}, data: png};
        if (action === 'upload_attachment') return {attachment: {id: 'up-' + data.name, name: data.name, type: data.type, size: 10, sha256: 'c'.repeat(64), image: data.type.startsWith('image/')}};
        if (action === 'draft_attach') {
          if (data.revision !== draft.revision) throw new Error('This draft changed. Refresh it before saving.');
          const file = data.message_attachment_id ? window.snapshot.messages[0].attachments.find(item => item.id === data.message_attachment_id) : {id: data.upload_id, name: data.upload_id.slice(3), type: 'text/plain', size: 10, sha256: 'd'.repeat(64)};
          Object.assign(draft, {edited: data.text, revision: draft.revision + 1, attachments: [...draft.attachments, {...file, id: 'r-' + file.id}]});
          return {draft: structuredClone(draft)};
        }
        if (action === 'draft_detach') { Object.assign(draft, {edited: data.text, revision: draft.revision + 1, attachments: draft.attachments.filter(item => item.id !== data.attachment_id)}); return {draft: structuredClone(draft)}; }
        if (action === 'prepare_draft') return {ready: true, draft: structuredClone(draft)};
        if (action === 'revise_draft') return {started: true};
        return {};
      };
    }, PNG);
    await page.addScriptTag({content: source}); await page.evaluate(() => window.nexusEmail.refresh());
    const queued = await page.locator('.email-message').textContent();
    assert.match(queued, /2 attachments/, 'the list counts readable files only');
    await page.locator('.email-message').click();
    const incoming = page.locator('#emailIncomingFiles .email-file');
    assert.equal(await incoming.count(), 3);
    await page.waitForFunction(() => document.querySelector('#emailIncomingFiles img')?.src.startsWith('blob:'));
    assert.match(await incoming.nth(2).textContent(), /huge\.zip.*size limit/);
    // A file from the email goes back with the reply; the reply text is saved with it.
    await page.locator('#emailReply').fill('Here is the invoice back.');
    await incoming.nth(1).getByRole('button', {name: 'Attach to reply'}).click();
    await page.waitForFunction(() => window.calls.some(call => call.action === 'draft_attach'));
    const attach = (await page.evaluate(() => window.calls)).find(call => call.action === 'draft_attach').data;
    assert.deepEqual([attach.message_attachment_id, attach.text, attach.revision], ['pdf', 'Here is the invoice back.', 2]);
    await page.waitForFunction(() => document.querySelectorAll('#emailReplyFiles .email-file').length === 1);
    // A file picked from the computer is uploaded and attached too.
    await page.locator('#emailReplyAttach').setInputFiles({name: 'notes.txt', mimeType: 'text/plain', buffer: Buffer.from('Budget')});
    await page.waitForFunction(() => document.querySelectorAll('#emailReplyFiles .email-file').length === 2);
    const second = (await page.evaluate(() => window.calls)).filter(call => call.action === 'draft_attach')[1].data;
    assert.equal(second.upload_id, 'up-notes.txt');
    assert.equal(second.revision, 3, 'each change continues from the revision the last one saved');
    await page.locator('#emailReplyFiles .email-file').first().getByRole('button', {name: 'Remove'}).click();
    await page.waitForFunction(() => document.querySelectorAll('#emailReplyFiles .email-file').length === 1);
    // Pictures for the AI: added to the request, sent with Revise with AI, then cleared.
    await page.locator('#emailRevisionAttach').setInputFiles({name: 'mockup.png', mimeType: 'image/png', buffer: Buffer.from(PNG, 'base64')});
    await page.waitForFunction(() => document.querySelectorAll('#emailRevisionFiles .email-file').length === 1);
    await page.locator('#emailRevise').click();
    await page.waitForFunction(() => window.calls.some(call => call.action === 'revise_draft'));
    const revise = (await page.evaluate(() => window.calls)).find(call => call.action === 'revise_draft').data;
    assert.deepEqual(revise.prompt_attachments, ['up-mockup.png']);
    assert.equal(revise.instruction, '', 'files alone are a valid request');
    await page.waitForFunction(() => !document.querySelectorAll('#emailRevisionFiles .email-file').length);
    // The send review lists exactly what goes with the reply.
    await page.evaluate(() => { const draft = window.snapshot.drafts[0]; draft.revision += 1; draft.edited = 'Revised with the mockup in mind.'; });
    await page.evaluate(() => window.nexusEmail.refresh());
    await page.locator('#emailApprove').click();
    await page.waitForFunction(() => document.querySelector('#emailCompose')?.open);
    assert.match(await page.locator('#emailComposeFiles').textContent(), /^Attached: notes\.txt/);
  } finally { await browser.close(); }
});

test('Opening browser mail stored before its pictures were read fetches them once and shows progress', {skip: !executablePath, timeout: 60000}, async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const page = await browser.newPage(); await page.setContent('<main id="emailView"></main>');
    await page.evaluate(() => {
      window.calls = [];
      window.snapshot = {accounts: [{id: 'a', kind: 'browser_outlook', provider_route: 'assistant'}], providers: [], memories: [], drafts: [], operations: [],
        messages: [{id: 'old', account_id: 'a', sender: 'friend@example.test', subject: '(No subject)', body: '', attachments_unread: true}]};
      window.request = async (url, options) => {
        if (!options) return structuredClone(window.snapshot);
        const action = url.split('/').pop(); window.calls.push(action);
        if (action === 'load_attachments') { window.snapshot.operations = [{id: 'attachments:old', state: 'running'}]; return {started: true}; }
        return {};
      };
    });
    await page.addScriptTag({content: source}); await page.evaluate(() => window.nexusEmail.refresh());
    await page.locator('.email-message').click();
    await page.waitForFunction(() => /Reading the pictures/.test(document.querySelector('#emailIncomingFilesStatus').textContent));
    await page.evaluate(() => { window.snapshot.operations = [{id: 'attachments:old', state: 'completed'}]; delete window.snapshot.messages[0].attachments_unread; Object.assign(window.snapshot.messages[0], {attachments: [{id: 'p', name: 'picture-1.png', type: 'image/png', size: 3309788, sha256: 'e'.repeat(64), image: true, inline: true}], attachments_checked_at: 'now'}); });
    await page.evaluate(() => window.nexusEmail.refresh());
    await page.waitForFunction(() => document.querySelectorAll('#emailIncomingFiles .email-file').length === 1 && document.querySelector('#emailIncomingFilesStatus').hidden);
    await page.evaluate(() => window.nexusEmail.refresh());
    assert.equal((await page.evaluate(() => window.calls)).filter(action => action === 'load_attachments').length, 1);
  } finally { await browser.close(); }
});
