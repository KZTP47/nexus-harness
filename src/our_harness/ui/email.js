/* Email workspace: email content is always text, never executable markup. */
(function (host) {
  'use strict';
  function createEmailStudio(root, api, options = {}) {
    const doc = root.ownerDocument;
    const state = {snapshot: {}, localConnections: new Map(), pendingRevision: null, loaded: false, account: '', message: '', draft: '', dirty: false, busy: false, refreshing: false, refreshPromise: null, mutationRevision: 0, editRevision: null, editBase: null, memoryEdits: new Map(), memoryAdds: new Map(), newAccount: false, providerRepair: null, oauthRequest: '', registrationDirty: new Set(), modelByRoute: new Map(), oauthLinks: new Map(), seen: new Set(), baseline: false};
    const el = (tag, text, cls) => { const node = doc.createElement(tag); if (text !== undefined) node.textContent = text; if (cls) node.className = cls; return node; };
    const by = id => root.querySelector('#' + id);
    const field = (parent, id, title, type = 'text') => { const label = el('label', title); label.htmlFor = id; const input = el(type === 'textarea' ? 'textarea' : 'input'); input.id = id; if (type !== 'textarea') input.type = type; parent.append(label, input); return input; };
    const button = (parent, id, text, handler) => { const node = el('button', text); node.id = id; node.type = 'button'; node.addEventListener('click', handler); parent.append(node); return node; };
    const select = (parent, id, title) => { const label = el('label', title); label.htmlFor = id; const node = el('select'); node.id = id; parent.append(label, node); return node; };
    const HINTS = {
      emailAccount: 'Choose which connected mailbox this page shows.',
      emailNewAccount: 'Start setting up another mailbox. The current one stays connected.',
      emailProvider: 'Choose the connected Claude or Codex route that writes your drafts.',
      emailModel: 'Choose the model that route uses for drafting and revising.',
      emailRefreshModels: 'Ask each connected assistant for its current model list. This sends no model request.',
      emailProviderCheck: 'Check whether the chosen assistant is signed in, and open its sign-in window when it is not. This sends no model request.',
      emailProviderRepairConfirm: 'Confirm before the Claude command line is signed out and signed in again.',
      emailProviderRepairRun: 'Open Claude\u2019s own terminal to update, sign out and sign in. Nexus never sees your account or password.',
      emailConnectionMethod: 'Choose how Nexus reaches your mailbox. The matching setup card appears below.',
      'emailLocalOpen-browser_outlook': 'Open a Nexus browser window and sign in to Outlook there.',
      'emailLocalOpen-browser_gmail': 'Open a Nexus browser window and sign in to Gmail there.',
      'emailLocalOpen-classic_outlook': 'Look for mailboxes already set up in classic Outlook on this computer.',
      'emailLocalFinish-browser_outlook': 'Tell Nexus the Outlook sign-in is done so automatic checking can start.',
      'emailLocalFinish-browser_gmail': 'Tell Nexus the Gmail sign-in is done so automatic checking can start.',
      'emailBrowserMode-browser_outlook': 'Choose whether later checks show the browser window or keep it hidden.',
      'emailBrowserMode-browser_gmail': 'Choose whether later checks show the browser window or keep it hidden.',
      'emailBrowserModeSave-browser_outlook': 'Save that choice for future Outlook checks and approved replies.',
      'emailBrowserModeSave-browser_gmail': 'Save that choice for future Gmail checks and approved replies.',
      'emailAutoConnect-outlook': 'Sign in to Outlook through the Microsoft API using a registration Nexus already has.',
      'emailAutoConnect-gmail': 'Sign in to Gmail through the Google API using a registration Nexus already has.',
      'emailConnect-outlook': 'Sign in to Outlook using the client ID saved above.',
      'emailConnect-gmail': 'Sign in to Gmail using the client ID saved above.',
      'emailClientId-outlook': 'Application or client ID from your Microsoft app registration.',
      'emailClientId-gmail': 'Client ID from your Google desktop app registration.',
      emailTenant: 'Microsoft tenant to sign in against. Leave common unless your administrator says otherwise.',
      emailGoogleClientSecret: 'Client secret supplied with the Google registration, if there is one.',
      emailManagedPrepare: 'Download and start a private mailbox service on this computer.',
      emailManagedSignIn: 'Add a mailbox to that service and sign in to it.',
      emailManagedAccounts: 'Choose which mailbox on the service to connect.',
      emailManagedDiscover: 'List the mailboxes the service already knows about.',
      emailManagedConnect: 'Connect the mailbox chosen above to the selected assistant.',
      emailManagedUrl: 'Web address of the EmailEngine service, from your administrator.',
      emailManagedToken: 'Access token for that service. It is stored locally and never shown again.',
      emailReconnect: 'Sign in to the current mailbox again after its session expires.',
      emailDisconnect: 'Stop checking this mailbox. Saved mail and drafts stay on this computer.',
      emailAutoPoll: 'Let Nexus check this mailbox and prepare drafts on its own while it runs.',
      emailAutoSeconds: 'How long to wait between automatic inbox checks, in seconds.',
      emailSaveAssistant: 'Save the assistant, model and automatic-checking settings for this mailbox.',
      emailEngineStart: 'Start the local workflow engine that runs drafting and sending.',
      emailRefresh: 'Reload this page from Nexus now.',
      emailKind: 'Choose whether this mailbox reads IMAP or only holds imported mail.',
      emailAccountName: 'A name for this mailbox inside Nexus.',
      emailAddress: 'The address replies are sent from.',
      emailSync: 'Check the inbox once now instead of waiting for the next automatic check.',
      emailInboxSearch: 'Show only the messages whose subject or sender contains this text.',
      emailInboxSort: 'Choose the order the inbox list is shown in.',
      emailGenerate: 'Ask the assistant to write a first reply to the selected message.',
      emailReply: 'The version you approve. Edit it here or ask the assistant to revise it.',
      emailRevisionRequest: 'Describe the change you want; the assistant rewrites Your reply.',
      emailRevise: 'Send your change request and the current reply to the assistant.',
      emailLearn: 'Save what your edits teach the assistant when you approve this reply.',
      emailSave: 'Keep your edits without sending anything.',
      emailRevert: 'Throw away unsaved edits and restore the last saved reply.',
      emailDiscard: 'Delete this draft. The incoming message stays in the list.',
      emailApprove: 'Open the mail draft to review your reply before sending or exporting.',
      emailRetry: 'Try drafting again after a failed attempt.',
      emailResume: 'Finish an approved reply whose delivery step did not complete.',
      emailDownload: 'Save the exported reply as an .eml file you can open in your mail app.',
      emailRetryLearning: 'Try learning from this approved reply again. Nothing is resent.',
      emailCheckDelivery: 'Ask the mailbox service what happened to this reply. Nothing is resent.',
      emailVerifiedSendChecked: 'Tick this only after you have seen the reply in your own mailbox.',
      emailRecordVerifiedSend: 'Record that you checked the reply arrived. Nothing is resent.',
      emailManualLearningTab: 'Preferences you wrote or approved yourself.',
      emailAutomaticLearningTab: 'Preferences learned from your Revise with AI requests, kept per recipient.',
      emailSender: 'Address the imported message came from.',
      emailSubject: 'Subject line of the imported message.',
      emailBody: 'Text of the imported message.',
      emailRaw: 'Paste a complete .eml message instead of filling the fields above.',
      emailFile: 'Choose an .eml file to read the message from.'
    };
    function applyHints() { for (const node of root.querySelectorAll('[id]')) { const hint = HINTS[node.id]; if (hint && node.title !== hint) node.title = hint; } }
    const note = (text, error = false) => { by('emailNotice').textContent = text; by('emailNotice').classList.toggle('email-error', error); };
    const currentDraft = () => (state.snapshot.drafts || []).find(d => d.id === state.draft);
    const currentAccount = () => (state.snapshot.accounts || []).find(a => a.id === state.account);
    const canEditDraft = draft => draft?.status === 'review' || (draft?.status === 'approved' && !!draft.error && ['browser_outlook', 'browser_gmail'].includes(currentAccount()?.kind));
    let sendVersion = 'edited', versionDraft = '';

    let lastSnapshotAt = 0;
    let activeRequest = null;
    let deliveryConfirmationKey = '';
    const requestErrors = new Map();
    const learningRequests = new Map();
    function operation(kind) { const keys = kind === 'draft' ? ['generate', 'draft'] : kind === 'approve' ? ['finalize', 'approve'] : [kind]; const matches = keys.map(key => (state.snapshot.operations || []).find(item => (item.id || item.kind) === key + ':' + state.draft)).filter(Boolean); return matches.find(item => item.state === 'running') || matches[0]; }
    function running(kind) { return operation(kind)?.state === 'running'; }
    function timestamp(value) { if (typeof value === 'number') return value < 1e12 ? value * 1000 : value; const parsed = Date.parse(value); return Number.isFinite(parsed) ? parsed : null; }
    function feedback(parent, id) {
      const node = el('span', undefined, 'email-action-status'); node.id = id; node.setAttribute('role', 'status'); node.setAttribute('aria-live', 'polite');
      const icon = el('span', '', 'email-activity-icon'); icon.setAttribute('aria-hidden', 'true');
      const message = el('span', '', 'email-activity-message'); const elapsed = el('span', '', 'email-activity-elapsed'); elapsed.setAttribute('aria-hidden', 'true');
      node.append(icon, message, elapsed); parent.append(node); return node;
    }
    function renderActivity() {
      renderPollingFeedback();
      const draft = currentDraft();
      const automaticStatus = by('emailAutomaticLearningStatus');
      if (automaticStatus) {
        automaticStatus.textContent = ({learned: 'Automatic preferences saved for this recipient. Review them in the AUTOMATICALLY tab.', no_reusable_preferences: 'No reusable preference found in this revision.', no_recipient: 'No automatic preference saved because this reply has no single recipient.', failed: 'Your reply was revised, but automatic learning failed. ' + (draft?.automatic_learning_error || 'Open the AUTOMATICALLY tab to retry learning from the saved revision.')}[draft?.automatic_learning_status] || '');
        automaticStatus.hidden = !automaticStatus.textContent; automaticStatus.classList.toggle('email-error', draft?.automatic_learning_status === 'failed');
      }
      for (const [kind, id, verb] of [['draft', 'emailGenerateStatus', 'Creating draft'], ['revise', 'emailRevisionStatus', 'Revising your reply'], ['approve', 'emailSendStatus', 'Processing your approved reply']]) {
        const node = by(id); if (!node) continue;
        const op = operation(kind); const request = activeRequest?.kind === kind && activeRequest.draft === state.draft ? activeRequest : null;
        let phase = '', text = '', start = timestamp(op?.started_at);
        const failed = requestErrors.get(kind + ':' + state.draft);
        if (kind === 'approve' && draft?.status === 'delivery_unknown') { phase = 'unknown'; text = 'Sending is not confirmed. Check your mailbox; this reply will not be resent automatically.'; }
        else if (request) { phase = 'running'; text = request.preparing ? (kind === 'revise' ? 'Checking draft readiness…' : 'Checking mailbox and preparing the original reply…') : 'Requesting ' + (kind === 'draft' ? 'a draft' : kind === 'revise' ? 'AI revision' : 'approval') + '…'; start = request.started; }
        else if (kind === 'approve' && ['sent', 'exported'].includes(draft?.status)) { phase = 'complete'; text = draft.status === 'exported' ? 'Reply exported; sending is not confirmed.' : draft.delivery_status === 'user_confirmed' ? 'You verified this reply was sent.' : 'Send action accepted; recipient delivery is not confirmed.'; }
        else if (kind === 'draft' && draft?.status === 'review') { phase = 'complete'; text = 'Draft ready to review.'; }
        else if (op?.state === 'failed' || failed) { phase = 'failed'; text = failed || (op?.error ? 'Previous attempt: ' + op.error : 'This action failed. Your saved reply is preserved.'); }
        else if (op?.state === 'running') { phase = 'running'; const preparation = ['draft:', 'approve:'].some(prefix => String(op.id || op.kind).startsWith(prefix)); text = 'Last reported: ' + (preparation ? 'starting the workflow' : verb.toLowerCase()) + '…'; }
        else if (draft?.error && ((kind === 'draft' && draft.status === 'error') || (kind === 'approve' && draft.approved_at))) { phase = 'failed'; text = 'Previous attempt: ' + draft.error; }
        else if (kind === 'draft' && draft?.status === 'queued') { phase = 'queued'; text = 'Draft queued. Waiting for the workflow to start.'; }
        else if (kind === 'draft' && draft?.status === 'generating') { phase = 'waiting'; text = 'Draft generation was started. Waiting for a current activity report.'; }
        else if (kind === 'revise' && state.pendingRevision?.id === state.draft) { phase = 'queued'; text = 'Revision requested. Waiting for the AI workflow.'; }
        else if (kind === 'approve' && draft?.status === 'approved') { phase = 'queued'; text = 'Approved reply queued. Waiting for the delivery workflow.'; }
        else if (kind === 'approve' && draft?.status === 'sending') { phase = 'waiting'; text = 'Sending was started. Waiting for confirmation; do not submit again.'; }
        else if (kind === 'approve' && draft?.status === 'submitted') { phase = 'queued'; text = 'Queued with the mailbox service. Sending is not confirmed yet.'; }
        else if (kind === 'revise' && op?.state === 'completed') { phase = 'complete'; text = 'AI revision completed. Review the updated text in Your reply.'; }
        // A fresh service snapshot reports operation state, not provider health.
        // Lifecycle updated_at is not a heartbeat; only explicit heartbeat_at is one.
        const heartbeat = timestamp(op?.heartbeat_at);
        if (phase === 'running' && (Date.now() - (request?.started || lastSnapshotAt) > 20000 || op?.stale === true || (heartbeat && Date.now() - heartbeat > 30000))) { phase = 'stale'; text = 'Waiting for an updated status. Last reported: ' + verb.toLowerCase() + '. Refresh to check.'; }
        if (op?.state === 'stale') { phase = 'stale'; text = 'Activity status is out of date. Refresh to check before trying again.'; }
        node.hidden = !text; node.dataset.phase = phase; node.classList.toggle('email-error', phase === 'failed' || phase === 'unknown');
        node.querySelector('.email-activity-icon').classList.toggle('is-running', phase === 'running');
        const label = node.querySelector('.email-activity-message'); if (label.textContent !== text) label.textContent = text;
        node.querySelector('.email-activity-elapsed').textContent = start && ['running', 'stale', 'waiting'].includes(phase) ? ' ' + Math.max(0, Math.floor((Date.now() - start) / 1000)) + 's elapsed' : '';
      }
    }
    function renderPollingFeedback() {
      const account = currentAccount();
      const settings = by('emailPollingSettingsStatus'); const timing = by('emailPollingTiming');
      if (!settings || !timing) return;
      const poll = state.snapshot.polling?.contract === 'email-polling/v1' ? state.snapshot.polling.accounts?.find(item => item.account_id === state.account) : null;
      const saved = poll?.interval_seconds ?? account?.poll_seconds;
      const enabled = poll?.enabled ?? account?.poll_enabled;
      const changed = account && (Number(by('emailAutoSeconds').value) !== saved || by('emailAutoPoll').checked !== !!enabled);
      settings.textContent = account ? (changed ? 'Unsaved changes. ' : '') + (enabled ? 'Saved check interval: ' + saved + ' seconds.' : 'Automatic checking is saved as off.') : '';
      let text = account && enabled ? 'Saved interval: ' + saved + 's. ' : account ? 'Automatic checking is off. ' : '';
      if (poll) {
        if (Date.now() - lastSnapshotAt > 20000) text += 'Waiting for an updated scan status. ';
        else if (poll.scan_running) text += 'A check is in progress; checks do not overlap. ';
        else if (enabled && poll.next_check_in_seconds != null) text += 'Next check due in ' + Math.max(0, Math.ceil(poll.next_check_in_seconds - (Date.now() - lastSnapshotAt) / 1000)) + 's. ';
        if (poll.last_duration_seconds != null) text += 'Last check took ' + Number(poll.last_duration_seconds).toFixed(1) + 's.';
      }
      timing.textContent = text;
    }
    const post = (action, body) => api('/api/email/' + action, {method: 'POST', body: JSON.stringify(body)});
    async function act(action, payload, success) {
      if (state.busy || !state.loaded) return;
      const kind = {create_draft: 'draft', retry_draft: 'draft', revise_draft: 'revise', approve_draft: 'approve', resume_draft: 'approve'}[action];
      if (kind) { activeRequest = {kind, draft: state.draft, started: Date.now()}; requestErrors.delete(kind + ':' + state.draft); }
      state.busy = true; state.mutationRevision += 1; controls(); note('Working…');
      try { const result = await post(action, payload); const completion = success ? await success(result) : null; if (state.refreshPromise) await state.refreshPromise; const fresh = await refresh(); if (fresh !== false) note(completion?.notice || (action === 'approve_draft' ? 'Approved. The workflow will complete delivery and learning; check the status below.' : 'Saved.')); else note('The action finished, but the email view could not be refreshed. It will retry shortly.', true); return result; }
      catch (error) { if (kind) requestErrors.set(kind + ':' + (activeRequest?.draft || state.draft), error.message); note(error.message, true); }
      finally { activeRequest = null; state.busy = false; controls(); }
    }
    async function preflightDraft(intent) {
      if (state.busy || !state.loaded) return false;
      const identity = state.draft, account = state.account, text = reply.value, base = state.editBase, wasDirty = state.dirty;
      const kind = intent === 'send' ? 'approve' : 'revise';
      state.busy = true; state.dirty = true; state.mutationRevision += 1;
      activeRequest = {kind, draft: identity, started: Date.now(), preparing: true};
      requestErrors.delete(kind + ':' + identity); controls();
      try {
        if (state.refreshPromise) await state.refreshPromise;
        await refresh();
        const draft = currentDraft();
        if (state.account !== account || draft?.id !== identity || !['review', 'approved'].includes(draft.status)) throw new Error('This reply is no longer available for editing or sending. Your text is preserved.');
        // Metadata-only changes and an already-saved copy can be reconciled.
        // A genuinely different saved edit must not be silently overwritten.
        if (state.editRevision !== draft.revision && draft.edited !== base && draft.edited !== text) throw new Error(wasDirty ? 'A different version was saved while you were editing. Your text is preserved. Review the saved version before replacing it.' : 'This reply was updated elsewhere. The newest saved version is shown now; review it and try again.');
        state.editRevision = draft.revision; state.editBase = draft.edited;
        const result = await post('prepare_draft', {account_id: account, draft_id: identity, revision: draft.revision, intent, text: intent === 'send' && sendVersion === 'original' ? draft.original : text});
        const prepared = result.draft || draft;
        state.editRevision = prepared.revision; state.editBase = prepared.edited;
        // A refresh already in flight may predate prepare_draft; discard it and read again.
        state.mutationRevision += 1;
        if (state.refreshPromise) await state.refreshPromise;
        await refresh();
        requestErrors.delete('approve:' + identity); requestErrors.delete('revise:' + identity);
        return true;
      } catch (error) { requestErrors.set(kind + ':' + identity, error.message); note(error.message, true); return false; }
      finally {
        // Without edits of their own the user sees the newest saved text, not a lock-out until Revert.
        const restore = !wasDirty && reply.value === text && state.dirty;
        if (restore) state.dirty = false;
        activeRequest = null; state.busy = false;
        if (restore && currentDraft()?.edited !== text) render(); else controls();
      }
    }
    function controls() {
      root.querySelectorAll('button').forEach(button => { button.disabled = button.id !== 'emailRefresh' && !state.loaded; });
      for (const kind of ['browser_outlook', 'browser_gmail']) by('emailBrowserMode-' + kind).disabled = state.busy || !state.loaded;
      if (!state.loaded) return;
      root.querySelectorAll('button[data-work]').forEach(b => { b.disabled = state.busy; });
      by('emailComposeSend').disabled = state.busy || composeSending; by('emailComposeCancel').disabled = composeSending;
      const draft = currentDraft(); const editable = canEditDraft(draft);
      const revising = (!!state.pendingRevision && state.pendingRevision.id === state.draft) || running('revise');
      for (const id of ['emailSave', 'emailApprove', 'emailRevise']) by(id).disabled = state.busy || !editable || revising;
      by('emailRevisionRequest').disabled = state.busy || !editable || revising;
      by('emailDiscard').disabled = state.busy || revising || !['queued', 'review', 'error', 'approved', 'delivery_unknown'].includes(draft?.status); by('emailRevert').disabled = state.busy || revising || !state.dirty; by('emailReply').disabled = state.busy || !editable || revising; by('emailRetry').hidden = draft?.status !== 'error'; by('emailResume').hidden = draft?.status !== 'approved'; by('emailDownload').hidden = draft?.status !== 'exported'; by('emailRetryLearning').hidden = !draft?.learning_error;
      by('emailGenerate').disabled = state.busy || !state.message || !state.account || state.dirty;
      by('emailGenerate').disabled ||= running('draft') || ['queued', 'generating'].includes(draft?.status);
      by('emailApprove').disabled = state.busy || revising || running('approve') || (!editable && draft?.status !== 'approved');
      by('emailResume').hidden = true;
      for (const id of ['emailUseOriginal', 'emailUseEdited']) by(id).disabled = state.busy || !editable || revising || running('approve');
      showSendVersion();
      by('emailResume').disabled ||= running('approve');
      by('emailRetry').disabled ||= running('draft');
      by('emailSync').disabled = state.busy || !state.account || !['imap', 'outlook', 'gmail', 'browser_outlook', 'browser_gmail', 'classic_outlook', 'emailengine'].includes(currentAccount()?.kind);
      by('emailCheckDelivery').hidden = currentAccount()?.kind !== 'emailengine' || !['submitted', 'delivery_unknown'].includes(draft?.status);
      const mayConfirm = ['browser_outlook', 'browser_gmail'].includes(currentAccount()?.kind) && draft?.status === 'delivery_unknown';
      const confirmationKey = mayConfirm ? state.account + ':' + draft.id + ':' + draft.revision : '';
      if (confirmationKey !== deliveryConfirmationKey) { by('emailVerifiedSendChecked').checked = false; deliveryConfirmationKey = confirmationKey; }
      by('emailManualDeliveryConfirmation').hidden = !mayConfirm;
      by('emailVerifiedSendChecked').disabled = state.busy || !mayConfirm;
      by('emailRecordVerifiedSend').disabled = state.busy || !mayConfirm || !by('emailVerifiedSendChecked').checked;
      root.querySelectorAll('button[data-learning-draft]').forEach(b => { b.disabled = state.busy || b.dataset.learningRunning === 'true'; });
      by('emailProviderRepairPanel').hidden = !state.providerRepair;
      by('emailProviderRepairRun').disabled = state.busy || !state.providerRepair || !by('emailProviderRepairConfirm').checked;
      renderActivity();
    }
    const heading = el('div', undefined, 'email-heading'); heading.append(el('h1', 'Email assistant'), el('p', 'Turn incoming mail into reviewed replies. Your approved edits can guide future drafts.')); root.append(heading);
    const notice = el('p', 'Connect a mailbox or import an email to begin.', 'email-notice'); notice.id = 'emailNotice'; notice.setAttribute('role', 'status'); notice.setAttribute('aria-live', 'polite'); root.append(notice);
    const connect = el('section', undefined, 'email-connect');
    connect.append(el('h2', 'Connect your email'), el('p', 'Sign in with Outlook or Gmail, choose your AI assistant, and Nexus will check for new mail and prepare drafts automatically. Review and edit every reply before approving it. Browser connections send only the reply you explicitly approve. Classic Outlook connections export approved replies for you to send.'), el('p', 'Incoming message context and approved edits are sent to your selected AI provider to draft replies and learn preferences.', 'field-help'));
    const connectedStatus = el('p'); connectedStatus.id = 'emailConnectionStatus'; connectedStatus.setAttribute('role', 'status'); connect.append(connectedStatus);
    const quickFields = el('div', undefined, 'email-form email-quick-fields'); connect.append(quickFields);
    const connectors = el('div', undefined, 'email-connectors');
    const registrationPanels = {};
    async function beginSignIn(provider, reconnect = false, automatic = false) {
      if (state.busy) return;
      if (state.dirty) return note('Save or revert your reply before connecting a mailbox.', true);
      const connector = state.snapshot.oauth?.[provider];
      if (!automatic && !connector?.configured) { registrations.open = true; registrationPanels[provider].open = true; return note((provider === 'outlook' ? 'Outlook' : 'Gmail') + ' sign-in needs an app registration. Enter its client ID below, or use manual mailbox setup.', true); }
      const route = by('emailProvider').value;
      if (!route) return note('Choose a connected Claude or Codex assistant before signing in.', true);
      await act(automatic ? 'oauth_auto_connect' : 'oauth_start', {provider, provider_route: route, provider_model: by('emailModel').value, poll_enabled: true, poll_seconds: 300, ...(reconnect ? {account_id: state.account} : {})}, result => {
        const registration = result.registration; if (registration?.client_id) { by('emailClientId-' + provider).value = registration.client_id; if (provider === 'outlook') by('emailTenant').value = registration.tenant || 'common'; state.registrationDirty.delete(provider); }
        if (result.state === 'publisher_registration_required') { registrations.open = true; registrationPanels[provider].open = true; return {notice: result.message || 'A publisher app registration is needed before automatic sign-in is available.'}; }
        state.oauthRequest = result.request_id || ''; state.oauthLinks.delete(provider);
        if (result.authorization_url) {
          try { const url = new URL(result.authorization_url); const expected = provider === 'outlook' ? 'login.microsoftonline.com' : 'accounts.google.com'; if (url.protocol === 'https:' && url.hostname === expected && !url.username && !url.password) state.oauthLinks.set(provider, url.href); } catch (_) {}
        }
        return {notice: 'Finish signing in in your browser. This page will update when the mailbox is connected.'};
      });
    }
    for (const [provider, title] of [['outlook', 'Outlook'], ['gmail', 'Gmail']]) {
      const card = el('div', undefined, 'email-connector'); card.append(el('h3', title));
      const status = el('p'); status.id = 'emailOAuthStatus-' + provider; card.append(status);
      button(card, 'emailAutoConnect-' + provider, 'Connect ' + title + ' automatically', () => beginSignIn(provider, false, true)).dataset.work = '1';
      const fallback = el('a', 'Continue sign-in in your browser'); fallback.id = 'emailOAuthLink-' + provider; fallback.target = '_blank'; fallback.rel = 'noopener noreferrer'; fallback.hidden = true; card.append(fallback); connectors.append(card);
    }
    const method = select(connect, 'emailConnectionMethod', 'How would you like to connect?');
    const managedOption = el('option', 'Use a mailbox service — local setup or existing server'); managedOption.value = 'emailengine'; method.append(managedOption);
    for (const [value, title] of [['browser', 'Sign in through a browser — Outlook or Gmail'], ['classic', 'Use classic Outlook on this Windows computer'], ['api', 'Use Microsoft / Google API — app registration required']]) { const item = el('option', title); item.value = value; method.append(item); }
    method.value = 'browser';
    connect.append(el('p', 'Keep Nexus running and this computer awake for automatic inbox checks and AI drafts. The assistant remembers saved mail and approved preferences across restarts.', 'field-help'));
    const localPanel = el('div', undefined, 'email-connectors'); localPanel.id = 'emailLocalConnections';
    const localKinds = ['browser_outlook', 'browser_gmail', 'classic_outlook'];
    const localNames = {browser_outlook: 'Outlook in a browser', browser_gmail: 'Gmail in a browser', classic_outlook: 'Classic Outlook for Windows'};
    const browserModes = new Map();
    function localConnection(kind) {
      const account = currentAccount();
      if (account?.kind === kind && account.connector_id) {
        return (state.snapshot.local_connections || []).find(item => item.id === account.connector_id)
          || (state.localConnections.get(kind)?.id === account.connector_id ? state.localConnections.get(kind) : null)
          || {id: account.connector_id, provider: kind};
      }
      return state.localConnections.get(kind);
    }
    function modeKey(kind) { return localConnection(kind)?.id || kind; }
    async function useLocal(kind, finish = false, reconnect = false) {
      if (state.dirty) return note('Save or revert your reply before connecting a mailbox.', true);
      if (!by('emailProvider').value) return note('Choose an AI assistant before connecting.', true);
      const settings = {provider_route: by('emailProvider').value, provider_model: by('emailModel').value};
      if (kind === 'classic_outlook' && !finish) return act('local_discover', {}, result => { renderDiscovered(result.connections || []); return {notice: result.message || ((result.connections || []).length ? 'Choose the Outlook mailbox to connect.' : 'No classic Outlook mailbox was found. Open classic Outlook with a configured account, or choose browser sign-in.')} });
      const account = currentAccount();
      const connection = reconnect && account?.kind === kind && account.connector_id ? {id: account.connector_id, provider: kind} : state.localConnections.get(kind);
      if (finish && !connection?.id) return note('Open sign-in first.', true);
      await act(finish ? 'local_status' : 'local_open', finish ? {connection_id: connection.id, kind} : {provider: kind, ...(connection?.id ? {connection_id: connection.id} : {}), browser_mode: by('emailBrowserMode-' + kind).value, ...settings}, async result => {
        const found = result.connection;
        if (found) state.localConnections.set(kind, found);
        renderLocal();
        if (finish && found?.state === 'connected') {
          const connected = await post('local_connect', {connection_id: found.id, kind, ...settings, poll_enabled: true, poll_seconds: 60});
          state.account = connected.account?.id || state.account; state.newAccount = false; by('emailAutoPoll').checked = true; by('emailAutoSeconds').value = '60';
          return {notice: 'Mailbox connected. Nexus will check for new mail and prepare drafts while it is running.'};
        }
        return {notice: result.message || found?.message || (found?.state === 'connected' ? 'Sign-in detected. Choose Finish sign-in to enable automatic drafts.' : 'Finish signing in in the opened browser, then choose Finish sign-in here.')};
      });
    }
    function renderDiscovered(connections) {
      const list = by('emailClassicMailboxes'); list.replaceChildren();
      for (const connection of connections) button(list, '', 'Connect ' + (connection.email || connection.name || 'Outlook mailbox'), () => {
        if (state.dirty) return note('Save or revert your reply first.', true);
        return act('local_connect', {connection_id: connection.id, kind: 'classic_outlook', provider_route: by('emailProvider').value, provider_model: by('emailModel').value, poll_enabled: true, poll_seconds: 60}, result => { state.account = result.account?.id || state.account; state.newAccount = false; by('emailAutoPoll').checked = true; by('emailAutoSeconds').value = '60'; return {notice: 'Classic Outlook connected. Automatic inbox checks are enabled while Nexus is running.'}; });
      }).dataset.work = '1';
    }
    function renderLocal() {
      // Restore resumable sessions after restart without replacing an action's newer result.
      for (const connection of [...(state.snapshot.local_connections || [])].reverse()) {
        if (connection?.id && localKinds.includes(connection.provider) && !state.localConnections.has(connection.provider)) state.localConnections.set(connection.provider, connection);
      }
      for (const kind of localKinds) {
        by('emailLocalCard-' + kind).hidden = ['api', 'emailengine'].includes(method.value) || (method.value === 'classic') !== (kind === 'classic_outlook');
        const connection = localConnection(kind);
        by('emailLocalStatus-' + kind).textContent = connection ? connection.message || ({connected: 'Sign-in detected. Finish setup to enable automatic drafts.', pending: 'Waiting for you to sign in.', error: 'Connection failed. Try opening sign-in again.', reconnect_required: 'Please sign in again.'}[connection.state] || connection.state) : kind === 'classic_outlook' ? 'Use an account already configured in classic Outlook. New Outlook is not supported by this option.' : 'Sign in once in a dedicated Nexus browser. No client ID is required. Your existing browser login may not carry over. Reading conversations may mark messages as read.';
        if (kind !== 'classic_outlook') {
          by('emailLocalFinish-' + kind).hidden = !connection?.id;
          by('emailBrowserMode-' + kind).value = browserModes.get(modeKey(kind)) || connection?.browser_mode || 'headed';
          by('emailBrowserModeSave-' + kind).hidden = !connection?.id;
          by('emailBrowserModeStatus-' + kind).textContent = ['reconnect_required', 'sign_in_required'].includes(connection?.state)
            ? 'Open sign-in to verify or reconnect this session in a visible browser; your background preference is kept.'
            : 'Sign-in always opens a visible browser. After setup, inbox checks and approved replies use the saved mode. Background mode keeps the browser window hidden.';
        }
      }
      connectors.hidden = method.value !== 'api'; registrations.hidden = method.value !== 'api';
      by('emailManagedService').hidden = method.value !== 'emailengine';
    }
    for (const kind of localKinds) {
      const card = el('div', undefined, 'email-connector'); card.id = 'emailLocalCard-' + kind; card.append(el('h3', localNames[kind]));
      const status = el('p'); status.id = 'emailLocalStatus-' + kind; card.append(status);
      button(card, 'emailLocalOpen-' + kind, kind === 'classic_outlook' ? 'Find Outlook mailboxes' : 'Open ' + (kind === 'browser_outlook' ? 'Outlook' : 'Gmail') + ' sign-in', () => useLocal(kind)).dataset.work = '1';
      if (kind !== 'classic_outlook') {
        const picker = select(card, 'emailBrowserMode-' + kind, 'Browser mode after sign-in');
        for (const [value, title] of [['headed', 'Visible browser'], ['headless', 'Background browser (headless)']]) { const option = el('option', title); option.value = value; picker.append(option); }
        picker.addEventListener('change', () => browserModes.set(modeKey(kind), picker.value));
        const help = el('p', undefined, 'field-help'); help.id = 'emailBrowserModeStatus-' + kind; card.append(help);
        button(card, 'emailBrowserModeSave-' + kind, 'Save browser mode', () => {
          const connection = localConnection(kind);
          if (!connection?.id) return note('Open browser sign-in before saving its mode.', true);
          return act('local_mode', {kind, connection_id: connection.id, browser_mode: picker.value}, result => {
            if (result.connection) { state.localConnections.set(kind, result.connection); state.snapshot.local_connections = (state.snapshot.local_connections || []).map(item => item.id === result.connection.id ? result.connection : item); }
            browserModes.delete(modeKey(kind));
            return {notice: 'Browser mode saved. Future inbox checks and approved replies use this mode; reconnecting still opens visible sign-in.'};
          });
        }).dataset.work = '1';
        button(card, 'emailLocalFinish-' + kind, 'Finish sign-in', () => useLocal(kind, true)).dataset.work = '1';
      }
      else { const list = el('div'); list.id = 'emailClassicMailboxes'; card.append(list); }
      localPanel.append(card);
    }
    method.addEventListener('change', renderLocal);
    connect.append(localPanel, connectors);
    const managed = el('section', undefined, 'email-connector'); managed.id = 'emailManagedService';
    managed.append(el('h3', 'EmailEngine mailbox service'), el('p', 'Prepare a private service on this Windows computer, or use an existing server under Advanced. Local setup downloads verified EmailEngine and Redis files on first use. Then add your mailbox and connect it below. Outlook and Gmail API sign-in still require the service publisher’s app registration.'));
    const managedStatus = el('p'); managedStatus.id = 'emailManagedStatus'; managedStatus.setAttribute('role', 'status'); managed.append(managedStatus);
    button(managed, 'emailManagedPrepare', 'Set up local mailbox service', () => act('emailengine_prepare', {}, () => ({notice: 'Preparing the private mailbox service. First setup downloads its runtime; progress and errors appear below.'}))).dataset.work = '1';
    const managedSignInLink = el('a', 'Open mailbox sign-in'); managedSignInLink.id = 'emailManagedSignInLink'; managedSignInLink.hidden = true; managedSignInLink.target = '_blank'; managedSignInLink.rel = 'noopener noreferrer';
    button(managed, 'emailManagedSignIn', 'Add mailbox / sign in', () => act('emailengine_sign_in', {}, result => {
      const url = new URL(result.authorization_url); const base = new URL(state.snapshot.emailengine.url);
      if (url.origin !== base.origin || url.username || url.password) throw new Error('The service returned an unexpected sign-in address.');
      managedSignInLink.href = url.href; managedSignInLink.hidden = false;
      return {notice: 'Finish adding your mailbox in the service window, then choose Find service mailboxes. Provider configuration may be required before OAuth sign-in is available.'};
    })).dataset.work = '1'; managed.append(managedSignInLink);
    const managedAccounts = select(managed, 'emailManagedAccounts', 'Mailbox on the service');
    function showManagedAccounts(accounts) {
      const previous = managedAccounts.value; managedAccounts.replaceChildren();
      const empty = el('option', accounts.length ? 'Choose a mailbox' : 'No mailboxes found. Add an account to the service first.'); empty.value = ''; managedAccounts.append(empty);
      for (const account of accounts) { const id = account.account || account.id; if (!id) continue; const option = el('option', (account.email || account.name || id) + (account.state ? ' — ' + account.state : '')); option.value = id; managedAccounts.append(option); }
      if ([...managedAccounts.options].some(option => option.value === previous)) managedAccounts.value = previous;
    }
    showManagedAccounts([]);
    button(managed, 'emailManagedDiscover', 'Find service mailboxes', () => act('emailengine_accounts', {}, result => { showManagedAccounts(result.accounts || []); return {notice: 'Mailbox list refreshed. Choose an account to connect.'}; })).dataset.work = '1';
    button(managed, 'emailManagedConnect', 'Connect selected mailbox', () => {
      if (state.dirty) return note('Save or revert your reply before connecting a mailbox.', true);
      if (!managedAccounts.value) return note('Choose a mailbox from the service first.', true);
      if (!by('emailProvider').value) return note('Choose an AI assistant before connecting.', true);
      return act('emailengine_connect', {remote_account_id: managedAccounts.value, provider_route: by('emailProvider').value, provider_model: by('emailModel').value, poll_enabled: true, poll_seconds: 60}, result => { state.account = result.account?.id || state.account; state.newAccount = false; state.message = ''; state.draft = ''; by('emailAutoPoll').checked = true; by('emailAutoSeconds').value = '60'; return {notice: 'Mailbox connected. Automatic inbox checks are enabled while Nexus is running. Each reply still requires your approval.'}; });
    }).dataset.work = '1';
    const managedAdvanced = el('details'); managedAdvanced.append(el('summary', 'Advanced: configure mailbox service'));
    managedAdvanced.append(el('p', 'Use the service address and access token supplied by your administrator. These belong to the mailbox service, not your Microsoft or Google app registration.'));
    const managedForm = el('form', undefined, 'email-form');
    const managedUrl = field(managedForm, 'emailManagedUrl', 'EmailEngine service address', 'url'); managedUrl.required = true;
    const managedToken = field(managedForm, 'emailManagedToken', 'Service access token', 'password'); managedToken.autocomplete = 'new-password'; managedToken.required = true;
    const managedSave = el('button', 'Save service and find mailboxes'); managedSave.type = 'submit'; managedSave.dataset.work = '1'; managedForm.append(managedSave);
    managedForm.addEventListener('submit', event => { event.preventDefault(); if (state.dirty) return note('Save or revert your reply before changing the mailbox service.', true); return act('emailengine_configure', {url: managedUrl.value.trim(), token: managedToken.value}, result => { managedToken.value = ''; showManagedAccounts(result.accounts || []); return {notice: 'Service configured. Choose a mailbox to connect.'}; }); });
    managedAdvanced.append(managedForm); managed.append(managedAdvanced); connect.append(managed);
    const connectionActions = el('div', undefined, 'email-connection-actions');
    button(connectionActions, 'emailReconnect', 'Reconnect mailbox', () => { const kind = currentAccount()?.kind; if (localKinds.includes(kind)) { method.value = kind === 'classic_outlook' ? 'classic' : 'browser'; renderLocal(); return useLocal(kind, false, true); } return beginSignIn(kind, true); }).dataset.work = '1';
    button(connectionActions, 'emailDisconnect', 'Disconnect mailbox', () => { if (state.dirty) return note('Save or revert your reply before disconnecting.', true); return act(['browser_outlook', 'browser_gmail', 'classic_outlook'].includes(currentAccount()?.kind) ? 'local_disconnect' : 'oauth_disconnect', {account_id: state.account}, () => ({notice: 'Mailbox disconnected. Saved mail and drafts remain available.'})); }).dataset.work = '1';
    const assistantSettings = el('div', undefined, 'email-form'); assistantSettings.id = 'emailOAuthAssistantSettings'; field(assistantSettings, 'emailAutoPoll', 'Automatically check mail and prepare drafts', 'checkbox');
    const interval = field(assistantSettings, 'emailAutoSeconds', 'Check interval (seconds)', 'number'); interval.value = '300'; interval.min = '1'; interval.max = '86400'; interval.step = '1'; interval.required = true;
    interval.addEventListener('input', renderPollingFeedback); assistantSettings.querySelector('#emailAutoPoll').addEventListener('change', renderPollingFeedback);
    button(assistantSettings, 'emailSaveAssistant', 'Save assistant settings', () => {
      if (!interval.reportValidity()) return note('Use a whole-number check interval from 1 to 86400 seconds.', true);
      return act('account_save', {account_id: state.account, provider_route: by('emailProvider').value, provider_model: by('emailModel').value, poll_enabled: by('emailAutoPoll').checked, poll_seconds: Number(interval.value)}, result => {
        if (result.account?.poll_seconds != null) { interval.value = result.account.poll_seconds; by('emailAutoPoll').checked = !!result.account.poll_enabled; }
        return {notice: 'Assistant settings saved.'};
      });
    }).dataset.work = '1';
    const savedPolling = el('p', '', 'field-help'); savedPolling.id = 'emailPollingSettingsStatus'; savedPolling.setAttribute('role', 'status'); assistantSettings.append(savedPolling, el('p', 'This sets when checks are due. Mail delivery, browser loading and AI drafting take additional time. If a check takes longer than the interval, the next check waits for it to finish.', 'field-help')); connect.append(connectionActions, assistantSettings);
    // Notifications belong to this project's mail workspace, not one mailbox, so
    // they stay visible for every kind of mailbox, including manual IMAP.
    const notifySettings = el('div', undefined, 'email-form'); notifySettings.id = 'emailNotifySettings'; connect.append(notifySettings);
    const notifyOn = field(notifySettings, 'emailNotifyNew', 'Corner notification when new mail arrives and a reply is being drafted', 'checkbox');
    const notifyDetails = field(notifySettings, 'emailNotifyDetails', 'Show sender and subject in that notification', 'checkbox');
    notifySettings.append(el('p', 'Turn off sender and subject when you share your screen. Manually imported mail is not announced.', 'field-help'));
    const saveNotify = () => { const saved = state.snapshot.notifications || {}; if (state.busy || !state.loaded) { notifyOn.checked = saved.enabled !== false; notifyDetails.checked = saved.show_details !== false; return note('Wait for the current action to finish.', true); } return act('notification_settings', {enabled: notifyOn.checked, show_details: notifyDetails.checked}, () => ({notice: !notifyOn.checked ? 'Corner notifications are off.' : notifyDetails.checked ? 'Corner notifications are on and show sender and subject.' : 'Corner notifications are on without sender or subject.'})); };
    notifyOn.addEventListener('change', saveNotify); notifyDetails.addEventListener('change', saveNotify);
    const registrations = el('details', undefined, 'email-registrations'); registrations.append(el('summary', 'Advanced: app registration for Outlook or Gmail'), el('p', 'The API connection method needs a registered app. Browser and classic Outlook connections above do not require these fields. Your organization or app distributor can provide the client ID. These settings do not sign in to a mailbox.'));
    for (const [provider, title] of [['outlook', 'Outlook'], ['gmail', 'Gmail']]) {
      const panel = el('details'); panel.append(el('summary', title + ' app registration')); registrationPanels[provider] = panel;
      const registrationForm = el('form', undefined, 'email-form');
      const clientId = field(registrationForm, 'emailClientId-' + provider, title + ' application / client ID'); clientId.required = true; clientId.addEventListener('input', () => state.registrationDirty.add(provider));
      if (provider === 'outlook') { const tenant = field(registrationForm, 'emailTenant', 'Microsoft tenant (common for personal and work accounts)'); tenant.value = 'common'; tenant.addEventListener('input', () => state.registrationDirty.add(provider)); }
      else { const secret = field(registrationForm, 'emailGoogleClientSecret', 'Google desktop app client secret (if supplied with the registration)', 'password'); secret.autocomplete = 'new-password'; }
      const save = el('button', 'Save ' + title + ' registration'); save.type = 'submit'; save.dataset.work = '1'; registrationForm.append(save);
      registrationForm.addEventListener('submit', event => { event.preventDefault(); const payload = {provider, client_id: by('emailClientId-' + provider).value.trim()}; if (provider === 'outlook') payload.tenant = by('emailTenant').value.trim() || 'common'; else if (by('emailGoogleClientSecret').value) payload.client_secret = by('emailGoogleClientSecret').value; act('oauth_configure', payload, () => { state.registrationDirty.delete(provider); if (provider === 'gmail') by('emailGoogleClientSecret').value = ''; return {notice: title + ' registration saved. Choose Connect ' + title + ' automatically to sign in.'}; }); });
      panel.append(registrationForm); button(panel, 'emailConnect-' + provider, 'Sign in with saved ' + title + ' registration', () => beginSignIn(provider)).dataset.work = '1'; registrations.append(panel);
    }
    connect.append(registrations); root.append(connect);
    const engine = el('section', undefined, 'email-engine'); engine.append(el('h2', 'Orchestration')); const status = el('p'); status.id = 'emailEngineStatus'; engine.append(status);
    button(engine, 'emailEngineStart', 'Start / retry Kestra', () => act('engine_start', {}, () => {})).dataset.work = '1';
    button(engine, 'emailRefresh', 'Refresh', () => refresh()); const operations = el('div'); operations.id = 'emailOperations'; engine.append(operations); root.append(engine);
    const setup = el('details', undefined, 'email-setup'); setup.open = false; setup.append(el('summary', 'Manual mailbox setup or imported mail'));
    const form = el('form', undefined, 'email-form'); form.id = 'emailAccountForm';
    const accountSelect = select(quickFields, 'emailAccount', 'Current mailbox');
    accountSelect.addEventListener('change', () => { if (state.busy) { accountSelect.value = state.account; return note('Wait for the current action to finish.', true); } if (state.dirty) { accountSelect.value = state.account; return note('Save or discard your edits before changing mailbox.', true); } state.newAccount = false; state.account = accountSelect.value; state.message = ''; state.draft = ''; fillAccount(); render(); });
    button(quickFields, 'emailNewAccount', 'New mailbox', () => { if (state.busy) return note('Wait for the current action to finish.', true); if (state.dirty) return note('Save or discard your edits first.', true); setup.open = true; state.newAccount = true; state.account = ''; state.message = ''; state.draft = ''; fillAccount(); render(); });
    const kind = select(form, 'emailKind', 'Mailbox type'); for (const [value, text] of [['import', 'Imported mail — export replies as .eml'], ['imap', 'IMAP inbox — deliver approved replies using SMTP']]) { const o = el('option', text); o.value = value; kind.append(o); }
    field(form, 'emailAccountName', 'Mailbox name'); field(form, 'emailAddress', 'Your email address', 'email');
    const providerPicker = select(quickFields, 'emailProvider', 'AI assistant (uses your connected Claude or Codex route)'); providerPicker.addEventListener('change', () => renderModels()); const modelPicker = select(quickFields, 'emailModel', 'Model'); modelPicker.addEventListener('change', () => state.modelByRoute.set(providerPicker.value, modelPicker.value)); button(quickFields, 'emailRefreshModels', 'Refresh models', () => act('refresh_models', {}, () => ({notice: 'Refreshing model catalogs. The model list will update when the check finishes.'}))).dataset.work = '1'; quickFields.append(el('p', 'Model availability depends on your connected CLI and account. Some Claude models require a newer Claude Code version.', 'field-help email-model-help'));
    const providerRepairBox = el('div', undefined, 'email-provider-repair');
    const providerRepairStatus = el('p', '', 'field-help'); providerRepairStatus.id = 'emailProviderRepairStatus'; providerRepairStatus.setAttribute('role', 'status'); providerRepairStatus.setAttribute('aria-live', 'polite');
    button(providerRepairBox, 'emailProviderCheck', 'Reconnect AI session', async () => {
      const route = by('emailProvider').value;
      if (!route) return note('Choose an AI assistant before checking its connection.', true);
      if (state.busy || !state.loaded) return;
      state.busy = true; controls(); note('Checking the AI connection\u2026');
      try {
        const plan = await api('/api/team/repair-plan', {method: 'POST', body: JSON.stringify({route})});
        const repair = plan.repair || {};
        const actions = (repair.actions || []).filter(action => action.route === route);
        const repairable = actions.find(action => action.id === 'repair-claude');
        state.providerRepair = repairable ? {route, fingerprint: repairable.diagnosis_fingerprint || repair.diagnosis_fingerprint || ''} : null;
        const summary = repair.summary || plan.note || '';
        // A route that is merely signed out needs its ordinary sign-in window, not the
        // sign-out repair, so pressing Reconnect opens that window straight away.
        if (actions.some(action => action.id === 'login')) {
          const opened = await api('/api/team/login', {method: 'POST', body: JSON.stringify({route})});
          providerRepairStatus.textContent = [summary, opened.note || 'Claude\u2019s own sign-in window is open. Finish there, then choose Reconnect AI session again.'].filter(Boolean).join(' ');
          providerRepairStatus.classList.remove('email-error');
          note('Finish signing in in Claude\u2019s window, then choose Reconnect AI session to check it again.');
        } else {
          providerRepairStatus.textContent = summary || 'This assistant reports no connection problem.';
          providerRepairStatus.classList.toggle('email-error', !!repairable);
          note(repairable ? 'This assistant needs a fresh sign-in. Confirm below to open Claude\u2019s own repair.' : 'Checked the AI connection.');
        }
      } catch (error) { state.providerRepair = null; providerRepairStatus.textContent = ''; note(error.message, true); }
      finally { state.busy = false; controls(); }
    }).dataset.work = '1';
    providerRepairBox.append(providerRepairStatus);
    const repairPanel = el('div'); repairPanel.id = 'emailProviderRepairPanel'; repairPanel.hidden = true;
    const repairConfirm = el('input'); repairConfirm.type = 'checkbox'; repairConfirm.id = 'emailProviderRepairConfirm'; repairConfirm.addEventListener('change', controls);
    const repairLabel = el('label', 'Sign the Claude command line out and sign in again'); repairLabel.htmlFor = repairConfirm.id;
    repairPanel.append(repairConfirm, repairLabel, el('p', 'Claude\u2019s own terminal window handles the update and sign-in. Nexus never sees your account, password or token. Finish anything open in Claude first.', 'field-help'));
    button(repairPanel, 'emailProviderRepairRun', 'Open Claude repair', async () => {
      const wanted = state.providerRepair;
      if (!wanted || !repairConfirm.checked || state.busy) return;
      state.busy = true; controls(); note('Opening Claude\u2019s repair window\u2026');
      try {
        const result = await api('/api/team/repair-claude', {method: 'POST', body: JSON.stringify({route: wanted.route, diagnosis_fingerprint: wanted.fingerprint})});
        repairConfirm.checked = false; state.providerRepair = null;
        providerRepairStatus.textContent = result.note || 'Claude\u2019s repair opened in its own terminal.';
        providerRepairStatus.classList.remove('email-error');
        note('Finish the sign-in in Claude\u2019s window, then choose Reconnect AI session to check it again.');
      } catch (error) { note(error.message, true); }
      finally { state.busy = false; controls(); }
    }).dataset.work = '1';
    providerRepairBox.append(repairPanel); quickFields.append(providerRepairBox);
    const connection = el('fieldset', undefined, 'email-connection'); connection.append(el('legend', 'Mailbox connection'));
    field(connection, 'emailImapHost', 'IMAP host'); field(connection, 'emailImapPort', 'IMAP port', 'number').value = '993'; field(connection, 'emailImapFolder', 'Folder').value = 'INBOX';
    field(connection, 'emailUsername', 'Username'); const password = field(connection, 'emailPassword', 'Password / app password (leave blank to keep saved secret)', 'password'); password.autocomplete = 'new-password';
    field(connection, 'emailSmtpHost', 'SMTP host'); field(connection, 'emailSmtpPort', 'SMTP port', 'number').value = '465';
    const mode = select(connection, 'emailSmtpMode', 'SMTP security'); for (const value of ['ssl', 'starttls']) { const o = el('option', value === 'ssl' ? 'TLS' : 'STARTTLS'); o.value = value; mode.append(o); }
    field(connection, 'emailPoll', 'Check inbox automatically', 'checkbox'); field(connection, 'emailPollSeconds', 'Check interval (seconds)', 'number').value = '300'; form.append(connection);
    const updateKind = () => { connection.hidden = kind.value !== 'imap'; }; kind.addEventListener('change', updateKind); updateKind();
    const saveAccount = el('button', 'Save mailbox'); saveAccount.type = 'submit'; saveAccount.dataset.work = '1'; form.append(saveAccount);
    form.addEventListener('submit', event => { event.preventDefault(); const payload = {account_id: state.account || undefined, name: by('emailAccountName').value, email: by('emailAddress').value, kind: kind.value, provider_route: by('emailProvider').value, provider_model: by('emailModel').value, imap_host: by('emailImapHost').value, imap_port: Number(by('emailImapPort').value), imap_folder: by('emailImapFolder').value, username: by('emailUsername').value, smtp_host: by('emailSmtpHost').value, smtp_port: Number(by('emailSmtpPort').value), smtp_mode: by('emailSmtpMode').value, poll_enabled: by('emailPoll').checked, poll_seconds: Number(by('emailPollSeconds').value)}; if (password.value) payload.password = password.value; act('account_save', payload, result => { state.newAccount = false; state.account = result.account?.id || result.id || state.account; password.value = ''; }); });
    setup.append(el('p', 'Use this fallback for another IMAP/SMTP provider, or create an imported-mail mailbox without connecting an inbox. Password or app-password access must be enabled for IMAP/SMTP; use browser sign-in above for Outlook and Gmail.', 'field-help')); setup.append(form); root.append(setup);
    const intake = el('details', undefined, 'email-import'); intake.append(el('summary', 'Import an email')); const importForm = el('form', undefined, 'email-form');
    field(importForm, 'emailSender', 'From', 'email'); field(importForm, 'emailSubject', 'Subject'); field(importForm, 'emailBody', 'Message', 'textarea'); const raw = field(importForm, 'emailRaw', 'Or paste a complete .eml message', 'textarea');
    const file = field(importForm, 'emailFile', 'Or choose an .eml file', 'file'); file.accept = '.eml,message/rfc822'; file.addEventListener('change', async () => { if (file.files[0]) raw.value = await file.files[0].text(); });
    const importButton = el('button', 'Import email'); importButton.type = 'submit'; importButton.dataset.work = '1'; importForm.append(importButton);
    importForm.addEventListener('submit', event => { event.preventDefault(); if (state.busy) return; if (state.dirty) return note('Save or revert your edits before importing another email.', true); if (!state.account) return note('Save a mailbox first.', true); act('import', {account_id: state.account, raw: raw.value || undefined, sender: by('emailSender').value, subject: by('emailSubject').value, body: by('emailBody').value}, result => { state.message = result.message?.id || ''; state.draft = (state.snapshot.drafts || []).find(d => d.message_id === state.message && d.account_id === state.account && !['discarded', 'sent', 'exported'].includes(d.status))?.id || ''; state.dirty = false; importForm.reset(); }); }); intake.append(importForm); root.append(intake);
    const columns = el('div', undefined, 'email-columns'); const queue = el('section', undefined, 'email-queue'); queue.append(el('h2', 'Inbox & drafts')); button(queue, 'emailSync', 'Check inbox now', () => act('sync', {account_id: state.account})); const inboxStatus = el('p'); inboxStatus.id = 'emailInboxStatus'; inboxStatus.setAttribute('role', 'status'); inboxStatus.setAttribute('aria-live', 'polite'); queue.append(inboxStatus); const pollingTiming = el('p', '', 'field-help'); pollingTiming.id = 'emailPollingTiming'; queue.append(pollingTiming);
    const filters = el('div', undefined, 'email-queue-filters');
    const inboxSearch = field(filters, 'emailInboxSearch', 'Search the inbox', 'search'); inboxSearch.placeholder = 'Subject or sender';
    const inboxSort = select(filters, 'emailInboxSort', 'Sort by');
    for (const [value, label] of [['newest', 'Newest first'], ['oldest', 'Oldest first'], ['sender', 'Sender A-Z'], ['subject', 'Subject A-Z']]) { const option = el('option', label); option.value = value; inboxSort.append(option); }
    inboxSearch.addEventListener('input', () => render()); inboxSort.addEventListener('change', () => render());
    queue.append(filters);
    const list = el('div'); list.id = 'emailQueue'; queue.append(list); columns.append(queue);
    const review = el('section', undefined, 'email-review'); review.append(el('h2', 'Review reply')); const incoming = el('pre', 'Choose an email.'); incoming.id = 'emailIncoming'; review.append(incoming);
    button(review, 'emailGenerate', 'Create draft', () => act('create_draft', {account_id: state.account, message_id: state.message, provider_route: by('emailProvider').value, provider_model: by('emailModel').value}, result => { state.draft = result.draft?.id || state.draft; })).dataset.work = '1';
    feedback(review, 'emailGenerateStatus');
    const draftStatus = el('p'); draftStatus.id = 'emailDraftStatus'; draftStatus.setAttribute('role', 'status'); review.append(draftStatus);
    const recipient = el('p'); recipient.id = 'emailReplyRecipient'; review.append(recipient);
    const comparison = el('div', undefined, 'email-comparison'); const originalBox = el('div'); originalBox.append(el('h3', 'Original AI draft'), el('p', 'Read-only initial suggestion, kept here for comparison. Edit the copy in Your reply.', 'field-help')); const original = el('pre'); original.id = 'emailOriginal'; original.setAttribute('aria-label', 'Read-only original AI suggestion'); originalBox.append(original); comparison.append(originalBox);
    const editedBox = el('div'); const reply = field(editedBox, 'emailReply', 'Your reply', 'textarea'); const replyHelp = el('p', 'Starts as a copy of the AI suggestion. Edit this text yourself or ask the AI to revise it. Select Send your reply to send this version.', 'field-help'); replyHelp.id = 'emailReplyHelp'; reply.setAttribute('aria-describedby', 'emailReplyHelp'); editedBox.insertBefore(replyHelp, reply); reply.rows = 14; reply.addEventListener('input', () => { state.dirty = true; sendVersion = 'edited'; showDiff(); controls(); }); comparison.append(editedBox); review.append(comparison);
    for (const [parent, id, value, title] of [[originalBox, 'emailUseOriginal', 'original', 'Send original AI draft'], [editedBox, 'emailUseEdited', 'edited', 'Send your reply']]) {
      const choice = el('input'); choice.type = 'radio'; choice.name = 'emailSendVersion'; choice.id = id; choice.value = value;
      const label = el('label', title, 'email-version-choice'); label.append(choice); parent.prepend(label);
      choice.addEventListener('change', () => { if (choice.checked) { sendVersion = value; showSendVersion(); } });
    }
    const versionNotice = el('p', '', 'email-version-notice'); versionNotice.id = 'emailSendVersionNotice'; versionNotice.setAttribute('role', 'status'); comparison.after(versionNotice);
    function showSendVersion() {
      if (versionDraft !== state.draft) { versionDraft = state.draft; sendVersion = 'edited'; }
      by('emailUseOriginal').checked = sendVersion === 'original'; by('emailUseEdited').checked = sendVersion === 'edited';
      originalBox.classList.toggle('email-selected-version', sendVersion === 'original'); editedBox.classList.toggle('email-selected-version', sendVersion === 'edited');
      versionNotice.textContent = 'Selected for sending: ' + (sendVersion === 'original' ? 'Original AI draft' : 'Your reply') + '. You will review this exact text in the mail window.';
    }
    const revisionBox = el('div', undefined, 'email-revision');
    field(revisionBox, 'emailRevisionRequest', 'Ask the AI to change this reply', 'textarea').placeholder = 'For example: make it shorter and friendlier, but keep the proposed date.';
    button(revisionBox, 'emailRevise', 'Revise with AI', async () => {
      let draft = currentDraft(); const instruction = by('emailRevisionRequest').value.trim();
      if (!draft || !instruction) return note('Describe the change you want the AI to make.', true);
      if (!await preflightDraft('revise')) return;
      draft = currentDraft();
      const submitted = {id: draft.id, text: reply.value, revision: state.editRevision ?? draft.revision};
      await act('revise_draft', {account_id: state.account, draft_id: draft.id, revision: submitted.revision, text: submitted.text, instruction, provider_route: by('emailProvider').value, provider_model: by('emailModel').value}, () => { state.pendingRevision = submitted; state.dirty = true; return {notice: 'The AI is revising your current reply. You can review the result before approving it.'}; });
    }); feedback(revisionBox, 'emailRevisionStatus'); const automaticStatus = el('p', '', 'field-help'); automaticStatus.id = 'emailAutomaticLearningStatus'; automaticStatus.setAttribute('role', 'status'); revisionBox.append(automaticStatus); review.append(revisionBox);
    const diff = el('pre'); diff.id = 'emailDiff'; diff.setAttribute('role', 'status'); review.append(diff);
    const learn = field(review, 'emailLearn', 'Learn from my edits', 'checkbox'); learn.checked = true;
    const oneOff = el('p', 'This controls learning from edits when you approve. Revise with AI learns separately in the AUTOMATICALLY tab. For a one-off AI change, say “for this reply only” in your request.', 'field-help'); review.append(oneOff);
    button(review, 'emailSave', 'Save edits', () => draftAction('save_draft')); button(review, 'emailRevert', 'Revert unsaved edits', () => { state.dirty = false; render(); note('Restored the saved reply.'); });
    button(review, 'emailDiscard', 'Discard draft', () => draftAction('discard_draft'));
    const compose = el('dialog', undefined, 'email-compose'); compose.id = 'emailCompose'; compose.setAttribute('aria-labelledby', 'emailComposeTitle');
    const composeTitle = el('h2', 'Review your email'); composeTitle.id = 'emailComposeTitle'; compose.append(composeTitle);
    const composeAddresses = el('div', undefined, 'email-compose-addresses');
    const fromBox = el('div'), toBox = el('div'); composeAddresses.append(fromBox, toBox); compose.append(composeAddresses);
    const composeFrom = field(fromBox, 'emailComposeFrom', 'From'); composeFrom.readOnly = true;
    const composeTo = field(toBox, 'emailComposeTo', 'To'); composeTo.readOnly = true;
    const composeSubject = field(compose, 'emailComposeSubject', 'Subject'); composeSubject.readOnly = true;
    const composeVersion = select(compose, 'emailComposeVersion', 'Version to send');
    for (const [value, title] of [['original', 'Original AI draft'], ['edited', 'Your reply']]) { const option = el('option', title); option.value = value; composeVersion.append(option); }
    // Edits made in this window to the original AI draft are kept per draft and
    // original text (not revision: Save edits bumps it but keeps the original), so
    // Escape, Cancel, switching versions or saving Your reply never loses them.
    const originalEdits = new Map();
    const textHash = text => { let hash = 0x811c9dc5; for (const char of String(text || '')) { hash ^= char.codePointAt(0); hash = Math.imul(hash, 0x01000193) >>> 0; } return hash.toString(16) + ':' + String(text || '').length; };
    const originalKey = draft => JSON.stringify([state.account, draft.id, textHash(draft.original)]);
    // The text a resumed (already approved) reply really sends is the saved one.
    const composeText = (draft, resume) => resume ? draft.edited : sendVersion === 'original' ? (originalEdits.get(originalKey(draft)) ?? draft.original) : reply.value;
    composeVersion.addEventListener('change', () => { sendVersion = composeVersion.value; composeBody.value = composeText(currentDraft(), false); showSendVersion(); });
    const composeBody = field(compose, 'emailComposeBody', 'Message', 'textarea'); composeBody.rows = 14;
    compose.append(el('p', 'From, To and Subject belong to the original conversation. Review the message before sending.', 'field-help'));
    let composeBinding = null, composeSending = false;
    composeBody.addEventListener('input', () => {
      // Editing "Your reply" here keeps it in step with the page. Editing the
      // original AI draft here changes only what this window sends, never the
      // unsaved "Your reply" text behind it.
      if (composeSending || composeBinding?.resume || composeBinding?.id !== state.draft || composeBinding.account !== state.account) return;
      if (sendVersion === 'edited') { reply.value = composeBody.value; state.dirty = true; showDiff(); }
      else {
        const draft = currentDraft(); if (!draft) return;
        originalEdits.set(originalKey(draft), composeBody.value);
        while (originalEdits.size > 20) originalEdits.delete(originalEdits.keys().next().value);
      }
    });
    const composeError = el('p'); composeError.setAttribute('role', 'alert'); compose.append(composeError);
    button(compose, 'emailComposeSend', 'Send', async () => {
      if (composeSending || state.busy || !composeBinding) return;
      const draft = currentDraft();
      if (state.account !== composeBinding.account || draft?.id !== composeBinding.id || draft?.revision !== composeBinding.revision || draft?.status !== composeBinding.status) {
        composeError.textContent = 'This draft changed. Cancel and reopen it to review the latest version.'; return;
      }
      if (!composeBody.value.trim()) { composeError.textContent = 'Enter a message before sending.'; return; }
      // Only the edited version mirrors into Your reply; sending the original AI
      // draft must not replace unsaved edits if the send fails or is cancelled.
      if (!composeBinding.resume && sendVersion === 'edited') { reply.value = composeBody.value; state.dirty = true; }
      composeSending = true; composeBody.disabled = true; composeVersion.disabled = true; controls();
      const result = composeBinding.resume ? await act('resume_draft', {account_id: state.account, draft_id: draft.id}) : await draftAction('approve_draft', composeBody.value);
      composeSending = false; composeBody.disabled = false; composeVersion.disabled = !!composeBinding?.resume;
      if (result) { originalEdits.delete(originalKey(draft)); compose.close(); }
      else composeError.textContent = by('emailNotice').textContent;
      controls();
    });
    button(compose, 'emailComposeCancel', 'Cancel', () => { if (!composeSending) compose.close(); });
    compose.addEventListener('cancel', event => { if (composeSending) event.preventDefault(); });
    compose.addEventListener('close', () => { composeBinding = null; by('emailApprove').focus(); });
    root.append(compose);
    button(review, 'emailApprove', 'Approve & export reply', async () => {
      if (!currentDraft() || state.busy || compose.open) return;
      if (!await preflightDraft('send')) return;
      const draft = currentDraft(), account = currentAccount();
      const message = (state.snapshot.messages || []).find(m => m.id === draft.message_id && m.account_id === state.account);
      if (!message) return note('The original message is unavailable.', true);
      composeBinding = {account: state.account, id: draft.id, revision: draft.revision, status: draft.status, resume: !canEditDraft(draft)};
      composeFrom.value = account?.email || ''; composeTo.value = message.reply_to || message.sender || '';
      composeSubject.value = /^re:/i.test(message.subject || '') ? message.subject : 'Re: ' + (message.subject || '');
      if (composeBinding.resume) sendVersion = 'edited';
      composeVersion.value = sendVersion; composeVersion.disabled = composeBinding.resume; composeBody.readOnly = composeBinding.resume;
      composeBody.value = composeText(draft, composeBinding.resume); composeError.textContent = '';
      showSendVersion();
      by('emailComposeSend').textContent = ['classic_outlook', 'import'].includes(account?.kind) ? 'Export reply' : 'Send';
      compose.showModal(); composeBody.focus();
    });
    feedback(review, 'emailSendStatus');
    const manualDelivery = el('div', undefined, 'email-manual-confirmation'); manualDelivery.id = 'emailManualDeliveryConfirmation'; manualDelivery.hidden = true;
    const verifiedSend = el('input'); verifiedSend.type = 'checkbox'; verifiedSend.id = 'emailVerifiedSendChecked'; verifiedSend.addEventListener('change', controls);
    const verifiedLabel = el('label', 'I checked Sent Items or the recipient confirmed this reply arrived'); verifiedLabel.htmlFor = verifiedSend.id;
    manualDelivery.append(verifiedSend, verifiedLabel, el('p', 'Record your verification only after checking this exact reply. This updates its status without sending it again.', 'field-help'));
    button(manualDelivery, 'emailRecordVerifiedSend', 'Record verified send', () => {
      const draft = currentDraft();
      if (!verifiedSend.checked || draft?.status !== 'delivery_unknown' || !['browser_outlook', 'browser_gmail'].includes(currentAccount()?.kind)) return;
      return act('confirm_browser_delivery', {account_id: state.account, draft_id: draft.id, revision: draft.revision, confirmation_contract: 'browser-delivery-confirmation/v1'}, () => { verifiedSend.checked = false; return {notice: 'Your verification was recorded. No reply was resent.'}; });
    }).dataset.work = '1';
    review.append(manualDelivery);
    button(review, 'emailRetry', 'Retry draft', () => act('retry_draft', {account_id: state.account, draft_id: state.draft, provider_route: by('emailProvider').value, provider_model: by('emailModel').value})).dataset.work = '1'; button(review, 'emailResume', 'Retry approved workflow', () => act('resume_draft', {account_id: state.account, draft_id: state.draft})).dataset.work = '1'; button(review, 'emailDownload', 'Download reply (.eml)', () => act('export_email', {account_id: state.account, draft_id: state.draft}, async result => {
      const filename = result.filename || 'reply.eml';
      if (typeof host.harnessDesktop?.saveEmailFile === 'function') {
        let saved;
        try { saved = await host.harnessDesktop.saveEmailFile(filename, result.content); }
        catch (error) { throw new Error('Could not save reply: ' + error.message); }
        return {notice: saved?.saved ? (saved.path ? 'Reply saved to ' + saved.path : 'Reply saved.') : 'Save cancelled. No file was saved.'};
      }
      const url = host.URL.createObjectURL(new Blob([result.content], {type: 'message/rfc822'}));
      const link = el('a'); link.href = url; link.download = filename; root.append(link); link.click(); link.remove();
      host.setTimeout(() => host.URL.revokeObjectURL(url), 1000);
      return {notice: 'Download requested. Check your browser downloads.'};
    })).dataset.work = '1'; button(review, 'emailRetryLearning', 'Retry learning (does not resend)', () => act('retry_learning', {account_id: state.account, draft_id: state.draft})).dataset.work = '1'; button(review, 'emailCheckDelivery', 'Check delivery status', () => act('check_delivery', {account_id: state.account, draft_id: state.draft}, () => ({notice: 'Delivery check requested. This page will update when it finishes; no reply is resent.'}))).dataset.work = '1'; const delivery = el('p'); delivery.id = 'emailDelivery'; review.append(delivery); columns.append(review); root.append(columns);
    const memory = el('section', undefined, 'email-memory');
    const memoryTabs = el('div', undefined, 'email-memory-tabs'); memoryTabs.setAttribute('role', 'tablist'); memoryTabs.setAttribute('aria-label', 'Assistant learning'); memory.append(memoryTabs);
    const manualPanel = el('div'); manualPanel.id = 'emailManualLearning'; manualPanel.setAttribute('role', 'tabpanel'); manualPanel.setAttribute('aria-labelledby', 'emailManualLearningTab');
    const automaticPanel = el('div'); automaticPanel.id = 'emailAutomaticLearning'; automaticPanel.setAttribute('role', 'tabpanel'); automaticPanel.setAttribute('aria-labelledby', 'emailAutomaticLearningTab'); automaticPanel.hidden = true;
    const tabs = [];
    function chooseMemoryTab(index, focus = false) { tabs.forEach((tab, i) => { tab.setAttribute('aria-selected', String(i === index)); tab.tabIndex = i === index ? 0 : -1; }); manualPanel.hidden = index !== 0; automaticPanel.hidden = index !== 1; if (focus) tabs[index].focus(); }
    for (const [index, id, title, panel] of [[0, 'emailManualLearningTab', 'What your assistant has learned', manualPanel], [1, 'emailAutomaticLearningTab', 'What your assistant has learned AUTOMATICALLY', automaticPanel]]) {
      const tab = button(memoryTabs, id, title, () => chooseMemoryTab(index)); tab.setAttribute('role', 'tab'); tab.setAttribute('aria-controls', panel.id); tabs.push(tab);
      tab.addEventListener('keydown', event => { if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) { event.preventDefault(); chooseMemoryTab(event.key === 'Home' ? 0 : event.key === 'End' ? 1 : 1 - index, true); } });
    }
    chooseMemoryTab(0);
    manualPanel.append(el('h2', 'What your assistant has learned'), el('p', 'Preferences are local to this mailbox. Incoming messages also provide context for related replies.'));
    const memories = el('div'); memories.id = 'emailMemories'; manualPanel.append(memories);
    const memoryForm = el('form'); const memoryText = field(memoryForm, 'emailMemoryText', 'Add a preference', 'textarea');
    memoryText.addEventListener('input', () => state.memoryAdds.set(state.account, memoryText.value));
    const addMemory = el('button', 'Save preference'); addMemory.type = 'submit'; addMemory.dataset.work = '1'; memoryForm.append(addMemory);
    memoryForm.addEventListener('submit', event => { event.preventDefault(); const account = state.account; act('memory_save', {account_id: account, text: memoryText.value}, () => { state.memoryAdds.delete(account); memoryText.value = ''; }); }); manualPanel.append(memoryForm);
    automaticPanel.append(el('h2', 'What your assistant has learned AUTOMATICALLY'), el('p', 'Each successful Revise with AI can learn reusable writing preferences from your request. Learning happens automatically; you can review, edit, or delete it here.'), el('p', 'Automatic preferences are kept separately for each recipient in this mailbox and used only when replying to that recipient. A customer’s preferences stay separate from your family’s.'));
    const automaticMemories = el('div'); automaticMemories.id = 'emailAutomaticMemories'; automaticPanel.append(automaticMemories); memory.append(manualPanel, automaticPanel); root.append(memory);
    function renderMemories(container, records, automatic) {
      // Keep focus/caret during polling, but never leave another mailbox's cards visible.
      if (container.dataset.account === state.account && container.contains(doc.activeElement) && doc.activeElement.tagName === 'TEXTAREA') return;
      container.dataset.account = state.account; container.replaceChildren();
      const active = records.filter(m => m.account_id === state.account && (!m.status || m.status === 'active'));
      const outcomes = automatic ? (state.snapshot.automatic_learning_outcomes || []).filter(item => item.account_id === state.account) : [];
      if (automatic && !active.length && !outcomes.length) container.append(el('p', state.account ? 'No automatic preferences yet. Use Revise with AI on a reply to start learning for that recipient.' : 'Choose a mailbox to see its automatic preferences.', 'email-memory-empty'));
      const groups = new Map();
      function recipientGroup(recipient) {
        const name = recipient || 'Recipient unavailable';
        if (!groups.has(name)) { const group = el('section', undefined, 'email-recipient-memory'); group.append(el('h3', name)); groups.set(name, group); container.append(group); }
        return groups.get(name);
      }
      for (const outcome of outcomes) {
        const group = recipientGroup(outcome.recipient);
        const op = (state.snapshot.operations || []).find(item => (item.id || item.kind) === 'automatic-learning:' + outcome.draft_id);
        const key = JSON.stringify([state.account, outcome.draft_id, outcome.request_fingerprint]);
        const request = learningRequests.get(key);
        const inProgress = request === 'running' || op?.state === 'running';
        const attempt = el('div', undefined, 'email-learning-attempt'); group.append(attempt);
        if (outcome.requested_change) attempt.append(el('p', 'Requested change: ' + outcome.requested_change));
        const messages = {
          not_recorded: 'This earlier revision has no recorded learning outcome. You can learn from its saved request.',
          learned: 'Reusable preferences were learned for this recipient.',
          no_reusable_preferences: 'The AI checked your revision but found no reusable preference. You can retry learning from the saved revision.',
          failed: 'Automatic learning failed. You can retry learning from the saved revision.',
          no_recipient: 'Learning could not be saved because this reply has no single recipient.',
          obsolete: 'This learning attempt belongs to an earlier assistant configuration. Revise a reply to learn with the current settings.'
        };
        const failed = request && request !== 'running' ? request : op?.state === 'failed' ? (op.error || 'Learning retry failed. Try again.') : '';
        const status = el('p', inProgress ? 'Learning from the saved revision…' : failed || messages[outcome.status] || 'Learning outcome unavailable.', 'email-learning-outcome');
        status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
        status.classList.toggle('email-error', !inProgress && (!!failed || outcome.status === 'failed')); attempt.append(status);
        if (outcome.retry_available) {
          const account = state.account;
          const retry = button(attempt, '', 'Retry learning', async () => {
            if (state.busy || inProgress) return;
            learningRequests.set(key, 'running');
            renderMemories(container, records, true);
            await act('retry_automatic_learning', {account_id: account, draft_id: outcome.draft_id, revision: outcome.revision, request_index: outcome.request_index, request_fingerprint: outcome.request_fingerprint}, () => {
              learningRequests.delete(key); return {notice: 'Learning requested. Your saved reply is unchanged.'};
            });
            if (learningRequests.get(key) === 'running') learningRequests.set(key, by('emailNotice').textContent || 'Learning retry failed. Try again.');
            renderMemories(container, state.snapshot.automatic_memories || [], true); controls();
          });
          retry.dataset.work = '1'; retry.dataset.learningDraft = outcome.draft_id; retry.dataset.learningRunning = String(inProgress);
          retry.disabled = state.busy || inProgress;
        }
      }
      for (const m of active) {
        let parent = container;
        if (automatic) {
          parent = recipientGroup(m.recipient);
        }
        const account = state.account; const key = JSON.stringify([account, automatic, m.id]);
        const card = el('div', undefined, 'email-memory-card'); const input = field(card, 'email-memory-' + (automatic ? 'automatic-' : '') + m.id, automatic ? 'Automatic preference' : 'Learned preference', 'textarea');
        input.value = state.memoryEdits.get(key) ?? m.text; input.addEventListener('input', () => state.memoryEdits.set(key, input.value));
        card.append(el('small', (m.source_draft_id ? (automatic ? 'From AI revision of draft ' : 'From reviewed draft ') + m.source_draft_id + (m.source_revision != null ? ', source revision ' + m.source_revision : '') : 'Added by you') + (m.revision != null ? ' · Preference revision ' + m.revision : '') + ' · ' + (m.status || 'active')));
        const save = button(card, '', 'Save', () => act('memory_save', {account_id: account, memory_id: m.id, text: input.value}, () => state.memoryEdits.delete(key))); save.dataset.work = '1';
        const remove = button(card, '', 'Delete', () => act('memory_delete', {account_id: account, memory_id: m.id}, () => state.memoryEdits.delete(key))); remove.dataset.work = '1';
        parent.append(card);
      }
    }
    function fillAccount() { const a = currentAccount() || {}; const fields = {emailAccountName: 'name', emailAddress: 'email', emailKind: 'kind', emailProvider: 'provider_route', emailImapHost: 'imap_host', emailImapPort: 'imap_port', emailImapFolder: 'imap_folder', emailUsername: 'username', emailSmtpHost: 'smtp_host', emailSmtpPort: 'smtp_port', emailSmtpMode: 'smtp_mode', emailPollSeconds: 'poll_seconds'}; const defaults = {emailKind: 'import', emailImapPort: 993, emailImapFolder: 'INBOX', emailSmtpPort: 465, emailSmtpMode: 'ssl', emailPollSeconds: 300}; for (const [id, key] of Object.entries(fields)) by(id).value = a[key] ?? defaults[id] ?? ''; if (a.provider_route) state.modelByRoute.set(a.provider_route, a.provider_model || (state.snapshot.providers || []).find(p => p.id === a.provider_route)?.model || ''); renderModels(); by('emailAutoPoll').checked = !!a.poll_enabled; by('emailAutoSeconds').value = a.poll_seconds || 300; by('emailPoll').checked = !!a.poll_enabled; by('emailAccount').value = state.account; password.value = ''; updateKind(); }
    function renderModels() {
      const route = by('emailProvider').value;
      const provider = (state.snapshot.providers || []).find(item => item.id === route);
      const selected = state.modelByRoute.get(route) || provider?.model || '';
      const catalog = new Map();
      for (const entry of provider?.models || []) { const id = typeof entry === 'string' ? entry : entry.id; if (id) catalog.set(id, typeof entry === 'string' ? id : entry.label || entry.name || id); }
      if (provider?.model && !catalog.has(provider.model)) catalog.set(provider.model, provider.model);
      if (selected && !catalog.has(selected)) catalog.set(selected, selected + ' (saved selection)');
      const picker = by('emailModel'); picker.replaceChildren();
      for (const [id, label] of catalog) { const option = el('option', label === id ? id : label + ' (' + id + ')'); option.value = id; picker.append(option); }
      if (!catalog.size) { const option = el('option', 'Choose an AI assistant to see available models'); option.value = ''; picker.append(option); }
      else picker.value = selected || catalog.keys().next().value;
    }
    function renderConnections() {
      renderLocal();
      const service = state.snapshot.emailengine || {};
      if (state.managedServiceUrl !== service.url) { managedSignInLink.hidden = true; managedSignInLink.removeAttribute('href'); state.managedServiceUrl = service.url; }
      managedStatus.textContent = service.local_runtime?.message || service.message || (service.configured ? 'Mailbox service configured. Find its mailboxes to connect.' : 'Prepare a local service, or configure an existing service under Advanced.');
      if (service.accounts?.length) showManagedAccounts(service.accounts);
      if (!managedUrl.value && service.url) managedUrl.value = service.url;
      const oauth = state.snapshot.oauth || {};
      const account = currentAccount(); const isLocal = ['browser_outlook', 'browser_gmail', 'classic_outlook'].includes(account?.kind); const isOAuth = ['outlook', 'gmail'].includes(account?.kind); const connectedKind = isLocal || isOAuth || account?.kind === 'emailengine';
      by('emailReconnect').hidden = !connectedKind || account?.kind === 'emailengine';
      by('emailDisconnect').hidden = !connectedKind || account?.connection_state === 'disconnected';
      by('emailAccountForm').hidden = connectedKind; by('emailOAuthAssistantSettings').hidden = !connectedKind;
      by('emailConnectionStatus').textContent = !state.loaded ? 'Loading mailbox and assistant settings...' : account ? (account.name || account.email || 'Mailbox') + ' - ' + (connectedKind ? (account.session_state === 'sign_in_required' ? 'Signed out - Nexus opened the sign-in and reconnects by itself' : {connected: 'Connected', reconnect_required: 'Reconnect required', disconnected: 'Disconnected'}[account.connection_state] || 'Checking connection') : account.kind === 'import' ? 'Imported mail; replies are exported' : 'Manual IMAP mailbox') + (account.error ? ': ' + account.error : '') : 'Choose Outlook or Gmail to begin, or expand manual setup to import mail.';
      for (const provider of ['outlook', 'gmail']) {
        const connector = oauth[provider] || {}; if (!state.registrationDirty.has(provider) && typeof connector.client_id === 'string') { by('emailClientId-' + provider).value = connector.client_id; if (provider === 'outlook') by('emailTenant').value = connector.tenant || 'common'; }
        const pending = (oauth.pending || []).filter(item => item.provider === provider).at(-1);
        const message = !state.loaded ? 'Loading sign-in settings...' : pending && ['pending', 'error', 'expired'].includes(pending.state) ? pending.message || (pending.state === 'pending' ? 'Waiting for browser sign-in...' : 'Sign-in did not finish. Try connecting again.') : connector.message || (connector.configured ? 'Ready for browser sign-in.' : 'App registration needed before browser sign-in.');
        by('emailOAuthStatus-' + provider).textContent = message;
        const link = by('emailOAuthLink-' + provider);
        const url = state.oauthLinks.get(provider);
        link.hidden = !url || pending && pending.state !== 'pending';
        if (url) link.href = url;
        if (pending?.state === 'connected' && pending.id === state.oauthRequest) { state.oauthRequest = ''; state.oauthLinks.delete(provider); note('Mailbox connected. Automatic inbox checks and draft preparation are enabled.'); }
      }
    }
    function showDiff() {
      const old = String(currentDraft()?.original || '');
      const before = old.split('\n'); const after = reply.value.split('\n');
      let start = 0;
      while (start < before.length && start < after.length && before[start] === after[start]) start++;
      let endBefore = before.length; let endAfter = after.length;
      while (endBefore > start && endAfter > start && before[endBefore - 1] === after[endAfter - 1]) { endBefore--; endAfter--; }
      const diff = by('emailDiff');
      diff.textContent = (state.dirty ? 'Unsaved edits. ' : 'Saved. ') + (old === reply.value ? 'Reply matches the original.' : 'Changes (- original, + edited):');
      if (old !== reply.value) {
        for (const line of before.slice(start, endBefore)) diff.append(el('span', '\n- ' + line, 'email-diff-removed'));
        for (const line of after.slice(start, endAfter)) diff.append(el('span', '\n+ ' + line, 'email-diff-added'));
      }
    }

    async function draftAction(action, approvedText) { const d = currentDraft(); if (!d) return; const text = approvedText ?? reply.value; const browserSend = action === 'approve_draft' && ['browser_outlook', 'browser_gmail'].includes(currentAccount()?.kind); return await act(action, {account_id: state.account, draft_id: d.id, revision: state.editRevision ?? d.revision, text, learn: learn.checked, ...(browserSend ? {approval_contract: 'browser-send/v1'} : {})}, () => { state.dirty = false; }); }
    function render() {
      const s = state.snapshot;
      renderConnections();
      accountSelect.replaceChildren(); const empty = el('option', 'Choose / create a mailbox'); empty.value = ''; accountSelect.append(empty); for (const a of s.accounts || []) { const item = el('option', a.name || a.email); item.value = a.id; accountSelect.append(item); } accountSelect.value = state.account;
      const provider = by('emailProvider'); const chosenProvider = provider.value || currentAccount()?.provider_route; provider.replaceChildren(); for (const p of s.providers || []) { const item = el('option', [p.id, p.name, p.model].filter(Boolean).join(' - ')); item.value = p.id; provider.append(item); } if (chosenProvider) provider.value = chosenProvider;
      if (!(s.providers || []).length) { const item = el('option', 'Connect Claude or Codex in Settings'); item.value = ''; provider.append(item); }
      renderModels();
      const orchestration = s.orchestration || {}; by('emailEngineStatus').textContent = typeof orchestration === 'string' ? orchestration : [orchestration.status || orchestration.state || (orchestration.running ? 'Running' : orchestration.available ? 'Ready to start' : 'Not available'), orchestration.message || orchestration.detail || orchestration.error || ''].filter(Boolean).join(' — ');
      by('emailOperations').replaceChildren();
      const operationLabels = {generate: 'Creating draft', finalize: 'Processing approved reply', delivery: 'Checking delivery status', models: 'Refresh models', revise: 'Revising reply with AI', draft: 'Creating draft', sync: 'Checking inbox', approve: 'Resuming approved reply', engine: 'Starting Kestra', learning: 'Learning preferences', 'automatic-learning': 'Learning from saved revision', recovery: 'Checking interrupted workflow'};
      for (const hold of s.sign_in_holds || []) {
        // Held, not failed: the drafts start by themselves once the sign-in works.
        const line = el('p', 'Waiting for ' + hold.label + ' sign-in - ' + hold.drafts + ' draft' + (hold.drafts === 1 ? '' : 's') + ' will be written automatically once it answers again.');
        line.className = 'email-waiting'; by('emailOperations').append(line);
      }
      // One line per distinct problem, not one per email: the same failure on ten
      // drafts is one thing to fix. A failure whose draft has since moved on is gone.
      const draftStatus = new Map((s.drafts || []).map(d => [d.id, d.status]));
      const grouped = new Map();
      for (const operation of s.operations || []) {
        if (!['running', 'failed'].includes(operation.state)) continue;
        const [prefix, subject] = String(operation.kind || operation.action || operation.id || '').split(':');
        if (operation.state === 'failed' && ['generate', 'draft'].includes(prefix) && subject && draftStatus.has(subject) && draftStatus.get(subject) !== 'error') continue;
        const label = operationLabels[prefix] || 'Email workflow';
        const detail = operation.state === 'failed' ? 'Failed: ' + (operation.error || 'Check the workflow status and retry this action.') : 'In progress';
        const key = label + ' - ' + detail;
        grouped.set(key, {count: (grouped.get(key)?.count || 0) + 1, failed: operation.state === 'failed'});
      }
      for (const [text, entry] of grouped) {
        const line = el('p', text + (entry.count > 1 ? ' (\u00d7' + entry.count + ')' : ''));
        if (entry.failed) line.className = 'email-error';
        by('emailOperations').append(line);
      }
      const syncOperation = (s.operations || []).find(item => (item.id || item.kind) === 'sync:' + state.account);
      by('emailInboxStatus').textContent = syncOperation?.state === 'running' ? 'Checking inbox…' : syncOperation?.state === 'failed' ? syncOperation.error || 'Inbox check failed. Choose Check inbox now to retry.' : syncOperation?.state === 'completed' ? 'Inbox check completed' + (timestamp(syncOperation.finished_at) ? ' at ' + new Date(timestamp(syncOperation.finished_at)).toLocaleTimeString() : '') + '. Newest messages appear first.' : 'Newest messages appear first.';
      by('emailInboxStatus').classList.toggle('email-error', syncOperation?.state === 'failed');
      const notify = s.notifications || {}; by('emailNotifyNew').checked = notify.enabled !== false; by('emailNotifyDetails').checked = notify.show_details !== false; by('emailNotifyDetails').disabled = notify.enabled === false;
      if (currentAccount()?.sync_has_more && syncOperation?.state !== 'failed') by('emailInboxStatus').textContent = syncOperation?.state === 'running' ? 'Loading more inbox messages…' : 'More inbox messages remain. The next check continues where this one stopped.';
      // Keep the reader's place and keyboard focus: the list is rebuilt on every inbox refresh.
      const keptScroll = by('emailQueue').querySelector('.email-queue-scroll')?.scrollTop || 0;
      const focusedMessage = by('emailQueue').contains(doc.activeElement) ? doc.activeElement.dataset?.messageId || '' : '';
      by('emailQueue').replaceChildren();
      const received = m => timestamp(m.received_at) || timestamp(m.imported_at) || 0;
      const orders = {newest: (a, b) => received(b) - received(a), oldest: (a, b) => received(a) - received(b),
        sender: (a, b) => (a.sender || '').localeCompare(b.sender || '') || received(b) - received(a),
        subject: (a, b) => (a.subject || '').localeCompare(b.subject || '') || received(b) - received(a)};
      const held = (s.messages || []).filter(m => m.account_id === state.account && (!m.account_fingerprint || m.account_fingerprint === currentAccount()?.fingerprint));
      const query = (by('emailInboxSearch')?.value || '').trim().toLowerCase();
      const messages = held.filter(m => !query || ((m.subject || '') + ' ' + (m.sender || '')).toLowerCase().includes(query))
        .sort(orders[by('emailInboxSort')?.value] || orders.newest);
      const failures = (s.failed_imports || []).filter(m => m.account_id === state.account && m.account_fingerprint === currentAccount()?.fingerprint);
      const counted = query ? 'Showing ' + messages.length + ' of ' + held.length + ' imported message' + (held.length === 1 ? '' : 's')
        : held.length + ' imported message' + (held.length === 1 ? '' : 's');
      by('emailQueue').append(el('p', counted + (failures.length ? '; ' + failures.length + (failures.length === 1 ? ' message' : ' messages') + ' could not be imported.' : '.')));
      if (failures.length) {
        const details = el('details'); details.open = state.importIssuesOpen; details.addEventListener('toggle', () => { state.importIssuesOpen = details.open; });
        details.append(el('summary', 'Show import issues (' + failures.length + ')'));
        for (const failure of failures) {
          // Mailbox checks move past a message they could not import; only a browser
          // row that changes is read again. The note says so, and can be cleared.
          const line = el('p', 'Message could not be imported: ' + failure.error + ' It is still in your mailbox; open it there to reply. This note clears itself if a later check imports it.');
          const account = state.account;
          const dismiss = button(line, '', 'Dismiss', () => act('dismiss_failed_import', {account_id: account, failure_id: failure.id}, () => ({notice: 'Import note dismissed. The email itself was not changed.'})));
          dismiss.dataset.work = '1'; dismiss.className = 'email-inline-action'; dismiss.title = 'Remove this note. Nothing in your mailbox changes.';
          details.append(line);
        }
        by('emailQueue').append(details);
      }
      if (state.message && !held.some(message => message.id === state.message)) { state.message = ''; state.draft = ''; state.dirty = false; state.editRevision = null; state.pendingRevision = null; }
      // A draft the assistant starts after the email was opened must still reach the open email.
      if (state.message && !state.dirty && !currentDraft()) { const newest = (s.drafts || []).filter(d => d.message_id === state.message && d.account_id === state.account).reverse(); const adopted = newest.find(d => !['discarded', 'sent', 'exported'].includes(d.status)) || newest[0]; if (adopted) state.draft = adopted.id; }
      if (!held.length) by('emailQueue').append(el('p', 'No messages yet. Import an email or check your connected inbox.'));
      else if (!messages.length) by('emailQueue').append(el('p', 'No messages match your search. Clear the search box to see them all.'));
      const scroller = el('div', undefined, 'email-queue-scroll'); if (messages.length) by('emailQueue').append(scroller);
      const focusMessage = id => { for (const node of by('emailQueue').querySelectorAll('.email-message')) if (node.dataset.messageId === id) return node.focus({preventScroll: true}); };
      for (const m of messages) { const drafts = (s.drafts || []).filter(d => d.message_id === m.id && d.account_id === state.account); const newest = [...drafts].reverse(); const d = newest.find(d => !['discarded', 'sent', 'exported'].includes(d.status)) || newest[0]; const b = button(scroller, '', (m.subject || '(No subject)') + '\n' + m.sender + (d ? '\n' + d.status : ''), () => { if (state.busy) return note('Wait for the current action to finish.', true); if (state.dirty) return note('Save or discard your edits before opening another email.', true); state.message = m.id; state.draft = d?.id || ''; render(); focusMessage(m.id); }); b.className = 'email-message'; b.dataset.messageId = m.id; b.title = 'Open this message and the reply drafted for it.'; b.setAttribute('aria-pressed', String(state.message === m.id)); }
      scroller.scrollTop = keptScroll;
      if (focusedMessage) focusMessage(focusedMessage);
      const message = held.find(m => m.id === state.message); by('emailIncoming').textContent = message ? 'From: ' + message.sender + '\nSubject: ' + message.subject + '\n\n' + message.body : 'Choose an email.';
      const draft = currentDraft();
      const pending = state.pendingRevision;
      if (pending && draft?.id === pending.id) {
        const operation = (s.operations || []).find(item => (item.id || item.kind) === 'revise:' + pending.id);
        if (operation?.state === 'failed') { if (draft.edited === pending.text) state.editRevision = draft.revision; state.pendingRevision = null; note(operation.error || 'AI revision failed. Your edits are preserved.', true); }
        else if (draft.revision > pending.revision && draft.status === 'review' && operation?.state !== 'running') { if (reply.value === pending.text) state.dirty = false; state.pendingRevision = null; by('emailRevisionRequest').value = ''; }
      }
      by('emailOriginal').textContent = draft?.original || ''; if (!state.dirty) { reply.value = draft?.edited ?? draft?.original ?? ''; state.editRevision = draft?.revision ?? null; state.editBase = draft?.edited ?? draft?.original ?? ''; }
      by('emailDraftStatus').textContent = draft ? 'Status: ' + ({submitted: 'Queued with the mailbox service; sending is not yet confirmed', delivery_unknown: 'Sending outcome is uncertain. Check status; do not resend automatically.', sent: draft.delivery_status === 'user_confirmed' ? 'You verified this reply was sent' : draft.delivery_status === 'browser_confirmed' ? 'Mailbox UI accepted the send action; recipient delivery is not confirmed' : 'Accepted for sending; recipient delivery is not confirmed', exported: 'Exported only; sending is not confirmed'}[draft.status] || draft.status) + (draft.error ? '\n' + draft.error : '') + (draft.learning_error ? '\nLearning failed: ' + draft.learning_error : '') : 'No draft yet.';
      by('emailReplyRecipient').textContent = message ? 'Reply to: ' + (message.reply_to || message.sender) + (['classic_outlook', 'import'].includes(currentAccount()?.kind) ? '. Choose the version below, then review the exact message before exporting.' : '. Choose the version below, then review the exact message before sending.') : '';
      by('emailApprove').textContent = ['imap', 'outlook', 'gmail', 'emailengine', 'browser_outlook', 'browser_gmail'].includes(currentAccount()?.kind) ? 'Review & send reply' : 'Review & export reply';
      by('emailDelivery').textContent = draft?.status === 'sent' && draft.delivery_status === 'user_confirmed' ? 'Sent status is based on your confirmation.' : ['browser_outlook', 'browser_gmail'].includes(currentAccount()?.kind) && draft?.status === 'delivery_unknown' ? 'The browser could not confirm sending. Check the Sent folder in your mailbox before taking further action. Nexus will not automatically resend this reply.' : draft?.export_path ? 'Reply exported: ' + draft.export_path : (['imap', 'outlook', 'gmail', 'emailengine', 'browser_outlook', 'browser_gmail'].includes(currentAccount()?.kind) ? 'Approval authorizes this reply to be sent through the connected mailbox.' : 'Approval creates an .eml reply you can open in your mail application.'); showDiff();
      renderMemories(by('emailMemories'), s.memories || [], false);
      renderMemories(by('emailAutomaticMemories'), s.automatic_memories || [], true);
      if (memoryText.dataset.account !== state.account) { memoryText.dataset.account = state.account; memoryText.value = state.memoryAdds.get(state.account) || ''; }
      applyHints();
      controls();
    }
    async function refresh() { if (state.refreshing) return state.refreshPromise; state.refreshing = true; const revision = state.mutationRevision; state.refreshPromise = (async () => { try { const snapshot = await api('/api/email'); if (revision !== state.mutationRevision) return; state.snapshot = snapshot; lastSnapshotAt = timestamp(snapshot.captured_at) || Date.now(); state.loaded = true; if (!state.newAccount && !state.account && !by('emailAccountName').value && snapshot.accounts?.length === 1) { state.account = snapshot.accounts[0].id; render(); fillAccount(); }
      const ready = (snapshot.drafts || []).filter(d => d.status === 'review'); const nav = doc.querySelector('[data-view="email"]'); if (nav) { nav.textContent = 'Email assistant' + (ready.length ? ' (' + ready.length + (ready.length === 1 ? ' draft)' : ' drafts)') : ''); nav.title = ready.length ? ready.length + ' drafts ready for review' : 'Read incoming mail, review replies, and teach your assistant'; } const newlyReady = ready.filter(d => !state.seen.has(d.id)); for (const d of ready) state.seen.add(d.id); render(); if (state.baseline && newlyReady.length) note(newlyReady.length + ' new draft' + (newlyReady.length === 1 ? '' : 's') + ' ready for review.'); state.baseline = true; return true;
    } catch (error) { lastSnapshotAt = Date.now(); note(error.message, true); return false; } finally { state.refreshing = false; } })(); return state.refreshPromise; }
    render();
    const activityTimer = options.poll === false ? null : host.setInterval(renderActivity, 1000);
    const timer = options.poll === false ? null : host.setInterval(() => { const delay = currentAccount()?.poll_enabled ? Math.min(6000, Math.max(1000, Number(currentAccount().poll_seconds || 60) * 1000)) : 6000; if (!state.busy && Date.now() - lastSnapshotAt >= delay) refresh(); }, 1000);
    async function open(target = {}) {
      await refresh();
      const message = (state.snapshot.messages || []).find(m => m.id === target.message_id && (!target.account_id || m.account_id === target.account_id));
      if (!message) { note('That email is no longer in this mailbox.', true); return false; }
      if (state.message === message.id) { render(); return true; }
      const waiting = state.snapshot.notifications?.show_details === false ? 'A new email is waiting.' : 'A new email from ' + (message.sender || 'a sender') + ' is waiting.';
      if (compose.open) { note(waiting + ' Finish or cancel the reply you are reviewing to open it.'); return false; }
      if (state.busy || state.dirty) { note(waiting + ' Save or discard your edits to open it.'); return false; }
      if (state.account !== message.account_id && (state.newAccount || state.registrationDirty.size)) { note(waiting + ' Save or cancel the mailbox setup you are editing to open it.'); return false; }
      if (state.account !== message.account_id) { state.newAccount = false; state.account = message.account_id; fillAccount(); }
      const drafts = (state.snapshot.drafts || []).filter(d => d.message_id === message.id && d.account_id === message.account_id).reverse();
      state.message = message.id; state.draft = (drafts.find(d => !['discarded', 'sent', 'exported'].includes(d.status)) || drafts[0])?.id || '';
      render(); by('emailIncoming')?.scrollIntoView?.({block: 'nearest'});
      return true;
    }
    return {refresh, open, state, destroy() { if (timer) host.clearInterval(timer); if (activityTimer) host.clearInterval(activityTimer); }};
  }
  // Corner notifications ("new mail, the assistant is drafting a reply"), shown
  // whatever tab is open. The words are chosen here once, for both the desktop
  // pop-up and the in-page fallback a plain browser gets.
  const MOST_CARDS_AT_ONCE = 3;
  function noticeCard(notice) {
    const target = {account_id: notice.account_id || '', message_id: notice.message_id || '', draft_id: notice.draft_id || ''};
    if (notice.kind === 'summary') return {id: notice.id, title: notice.count + ' new emails', action: 'Nexus AI is drafting replies', detail: notice.account ? 'In ' + notice.account : 'Click to review them', avatar: notice.count > 99 ? '99' : String(notice.count), ...target};
    if (notice.private || !notice.sender) return {id: notice.id, title: 'New email', action: 'Nexus AI is drafting a reply', detail: notice.private ? 'Open Nexus Harness to read it' : (notice.subject || '(No subject)'), avatar: '@', ...target};
    return {id: notice.id, title: notice.sender, action: 'New email \u00b7 Nexus AI is drafting a reply', detail: notice.subject || '(No subject)', avatar: ([...notice.sender.trim()][0] || '@').toUpperCase(), ...target};
  }
  function cardsFor(notices) {
    if (notices.length <= MOST_CARDS_AT_ONCE) return notices.map(noticeCard);
    const newest = notices[notices.length - 1]; const accounts = new Set(notices.map(n => n.account));
    return [noticeCard({...newest, id: 'summary-' + newest.id, kind: 'summary', count: notices.length, account: accounts.size === 1 ? newest.account : ''})];
  }
  const NOTICE_CURSOR_KEY = 'nexus-mail-notice-cursor';
  function watchNotifications(api, show, options = {}) {
    const every = options.every || 4000; let boot = ''; let cursor = null; let failures = 0; let timer = null; let stopped = false;
    // The cursor survives a reload of this tab, so the last two minutes are not
    // replayed as new; a restarted server has a new boot mark and resets it.
    const storage = options.storage === undefined ? (() => { try { return host.sessionStorage; } catch (_) { return null; } })() : options.storage;
    try { const saved = JSON.parse(storage?.getItem(NOTICE_CURSOR_KEY) || 'null'); if (saved && typeof saved.boot === 'string' && saved.boot && Number.isInteger(saved.seq) && saved.seq >= 0) { boot = saved.boot; cursor = saved.seq; } } catch (_) { /* no saved cursor */ }
    const remember = () => { try { storage?.setItem(NOTICE_CURSOR_KEY, JSON.stringify({boot, seq: cursor})); } catch (_) { /* private mode or blocked storage */ } };
    async function tick() {
      timer = null; let feed = null;
      try { feed = await api('/api/email/notifications' + (cursor === null ? '' : '?after=' + cursor)); failures = 0; } catch (_) { failures += 1; }
      if (feed && Array.isArray(feed.items) && !stopped) {
        // A restarted server numbers from one again: read its whole feed once.
        if (cursor !== null && feed.boot !== boot) { cursor = null; boot = ''; timer = host.setTimeout(tick, 0); return; }
        const replay = Number(feed.replay_seconds) || 120;
        const fresh = feed.items.filter(item => cursor === null ? Number(item.age_seconds) <= replay : Number(item.seq) > cursor);
        boot = String(feed.boot || ''); cursor = Math.max(cursor || 0, Number(feed.seq) || 0);
        for (const card of cardsFor(fresh)) { try { await show(card); } catch (_) { /* one card must not stop the rest */ } }
        // Saved only once every card went somewhere (pop-up or page): a reload while
        // the pop-up still decides must offer those cards again, not skip them.
        if (!stopped) remember();
      }
      if (!stopped) timer = host.setTimeout(tick, every * Math.min(8, 2 ** failures));
    }
    timer = host.setTimeout(tick, 0);
    return {tick, stop() { stopped = true; if (timer) host.clearTimeout(timer); }};
  }
  function showInPage(doc, card, onOpen) {
    let stack = doc.getElementById('nexusMailToasts');
    if (stack && [...stack.children].some(node => node.dataset.id === card.id)) return;
    if (!stack) { stack = doc.createElement('div'); stack.id = 'nexusMailToasts'; stack.className = 'nexus-mail-toasts'; stack.setAttribute('role', 'log'); stack.setAttribute('aria-live', 'polite'); stack.setAttribute('aria-label', 'New email notifications'); doc.body.append(stack); }
    const span = (text, cls) => { const node = doc.createElement('span'); node.className = cls; node.textContent = text; return node; };
    const item = doc.createElement('div'); item.className = 'nexus-mail-toast'; item.dataset.id = card.id;
    const openButton = doc.createElement('button'); openButton.type = 'button'; openButton.className = 'nexus-mail-toast-open'; openButton.title = 'Open this email';
    const words = span('', 'nexus-mail-toast-text'); words.append(span(card.title, 'nexus-mail-toast-title')); if (card.action) words.append(span(card.action, 'nexus-mail-toast-action')); if (card.detail) words.append(span(card.detail, 'nexus-mail-toast-detail'));
    const avatar = span(card.avatar, 'nexus-mail-toast-avatar'); avatar.setAttribute('aria-hidden', 'true'); openButton.append(avatar, words);
    const close = doc.createElement('button'); close.type = 'button'; close.className = 'nexus-mail-toast-close'; close.textContent = '\u00d7'; close.setAttribute('aria-label', 'Dismiss notification');
    let timer = null; let remaining = 7000; let started = 0;
    const leave = () => { host.clearTimeout(timer); item.remove(); };
    const count = () => { started = Date.now(); timer = host.setTimeout(leave, remaining); };
    item.addEventListener('mouseenter', () => { host.clearTimeout(timer); remaining = Math.max(1200, remaining - (Date.now() - started)); });
    item.addEventListener('mouseleave', count); item.addEventListener('focusin', () => host.clearTimeout(timer)); item.addEventListener('focusout', count);
    openButton.addEventListener('click', () => { leave(); onOpen(card); }); close.addEventListener('click', leave);
    item.append(openButton, close); stack.append(item);
    const live = [...stack.children]; for (const old of live.slice(0, Math.max(0, live.length - MOST_CARDS_AT_ONCE))) old.remove();
    count();
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = {createEmailStudio, noticeCard, cardsFor, watchNotifications};
  if (host.document) {
    let studio; let watcher = null;
    const ensure = () => { const root = host.document.getElementById('emailView'); if (!studio) studio = createEmailStudio(root, (path, options) => request(path, options)); return studio; };
    const openFromNotice = async target => { if (!target?.account_id) return; /* a sign-in card: the window is focused and the banner shows it */ if (typeof host.switchView === 'function') host.switchView('email', {userInitiated: true}); return ensure().open(target); };
    host.nexusEmail = {
      refresh() { return ensure().refresh(); },
      open: openFromNotice,
      watch() {
        if (watcher) return watcher;
        const desktop = host.harnessDesktop;
        desktop?.onMailNotificationActivated?.(target => { void openFromNotice(target); });
        watcher = watchNotifications((path, options) => request(path, options), async card => {
          if (desktop?.showMailNotification) { try { if (await desktop.showMailNotification(card)) return; } catch (_) { /* fall back to the page */ } }
          showInPage(host.document, card, openFromNotice);
        });
        return watcher;
      },
    };
  }
})(typeof window !== 'undefined' ? window : globalThis);
