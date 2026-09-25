// App-wide sign-in health banner. The server watches every AI and mailbox
// sign-in in the background (session_health.py); this page only shows what it
// found, loudly, on every tab, and offers the user the controls to take over.
(function (host) {
  'use strict';
  const EVERY_MS = 5000;
  const RECOVERED_SHOWN_MS = 9000;

  function words(session) {
    if (session.verifying) return 'Checking that ' + session.label + ' answers again…';
    if (session.sign_in_opened_at && session.auto_sign_in) return 'Nexus opened its sign-in window. Finish signing in there; work continues by itself.';
    if (session.sign_in_opened_at) return 'The sign-in window is open. Finish signing in there; work continues by itself.';
    if (!session.auto_sign_in) return 'You chose to handle this sign-in yourself. Press Open sign-in when you are ready.';
    return 'Nexus is opening its sign-in for you.';
  }

  function createSessionBanner(doc, api, options = {}) {
    const desktop = options.desktop === undefined ? host.harnessDesktop : options.desktop;
    let cursor = null; let boot = ''; let timer = null; let failures = 0; let stopped = false;
    const recovered = new Map();
    const bar = doc.createElement('section');
    bar.id = 'nexusSessionBanner';
    bar.className = 'nexus-session-banner';
    bar.setAttribute('role', 'alert');
    bar.setAttribute('aria-live', 'assertive');
    bar.hidden = true;
    const anchor = doc.querySelector('header.topbar');
    if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(bar, anchor.nextSibling); else doc.body.prepend(bar);

    async function act(body) {
      try { await api('/api/session-health', {method: 'POST', body: JSON.stringify(body)}); } catch (error) { note = String(error?.message || error); }
      schedule(300);
    }
    let note = '';

    function button(label, onClick, cls = '') {
      const one = doc.createElement('button'); one.type = 'button'; one.textContent = label; if (cls) one.className = cls;
      one.addEventListener('click', onClick); return one;
    }

    function render(state) {
      bar.replaceChildren();
      const open = (state.sessions || []).filter(one => one.state === 'signed_out');
      const now = Date.now();
      for (const [key, until] of recovered) if (until < now) recovered.delete(key);
      if (!open.length && !recovered.size) { bar.hidden = true; return; }
      bar.hidden = false;
      for (const session of open) {
        const row = doc.createElement('div'); row.className = 'nexus-session-row nexus-session-problem';
        const text = doc.createElement('div'); text.className = 'nexus-session-text';
        const title = doc.createElement('strong'); title.textContent = '⚠ ' + session.label + ' needs you to sign in again.';
        const detail = doc.createElement('span'); detail.textContent = ' ' + words(session);
        text.append(title, detail);
        if (session.sign_in_note && !session.sign_in_opened_at) { const extra = doc.createElement('small'); extra.textContent = session.sign_in_note; text.append(extra); }
        if (session.reason) {
          const why = doc.createElement('details'); const summary = doc.createElement('summary'); summary.textContent = 'What it said';
          const said = doc.createElement('small'); said.textContent = session.reason; why.append(summary, said); text.append(why);
        }
        const buttons = doc.createElement('div'); buttons.className = 'nexus-session-actions';
        buttons.append(button(session.sign_in_opened_at ? 'Open sign-in again' : 'Open sign-in', () => act({action: 'open_sign_in', session: session.key}), 'primary'));
        buttons.append(button('Check now', () => act({action: 'check_now', session: session.key})));
        buttons.append(session.manual
          ? button('Let Nexus handle it', () => act({action: 'manual', session: session.key, manual: false}))
          : button('I’ll handle it', () => act({action: 'manual', session: session.key, manual: true})));
        row.append(text, buttons); bar.append(row);
      }
      for (const [key] of recovered) {
        const session = (state.sessions || []).find(one => one.key === key);
        if (!session || session.state === 'signed_out') continue;
        const row = doc.createElement('div'); row.className = 'nexus-session-row nexus-session-ok';
        row.textContent = '✓ ' + session.label + ' works again. Waiting work continues automatically.';
        bar.append(row);
      }
      const settings = doc.createElement('label'); settings.className = 'nexus-session-setting';
      const box = doc.createElement('input'); box.type = 'checkbox'; box.checked = state.settings?.auto_sign_in !== false;
      box.addEventListener('change', () => act({action: 'settings', auto_sign_in: box.checked}));
      settings.append(box, doc.createTextNode(' Open sign-in windows for me automatically'));
      if (open.length) bar.append(settings);
      if (note) { const problem = doc.createElement('small'); problem.className = 'nexus-session-note'; problem.textContent = note; bar.append(problem); note = ''; }
    }

    async function announce(item) {
      if (!['signed_out', 'recovered', 'sign_in_opened'].includes(item.kind)) return;
      if (item.kind === 'recovered') recovered.set(item.session, Date.now() + RECOVERED_SHOWN_MS);
      const card = {id: 'session-' + String(item.seq), title: item.title, action: item.detail, detail: '', avatar: item.kind === 'recovered' ? '✓' : '!'};
      if (desktop?.showMailNotification) { try { await desktop.showMailNotification(card); } catch (_) { /* the banner still shows it */ } }
    }

    async function tick() {
      timer = null;
      let state = null;
      try { state = await api('/api/session-health' + (cursor === null ? '' : '?after=' + cursor)); failures = 0; } catch (_) { failures += 1; }
      if (state && !stopped) {
        if (cursor !== null && state.boot !== boot) cursor = null;
        const fresh = (state.feed || []).filter(item => cursor === null ? Number(item.age_seconds) <= 120 : Number(item.seq) > cursor);
        boot = String(state.boot || ''); cursor = Math.max(cursor || 0, Number(state.seq) || 0);
        for (const item of fresh) await announce(item);
        render(state);
      }
      if (!stopped) schedule(EVERY_MS * Math.min(8, 2 ** failures));
    }
    function schedule(ms) { if (timer) host.clearTimeout(timer); timer = host.setTimeout(tick, ms); }
    schedule(0);
    return {tick, render, element: bar, stop() { stopped = true; if (timer) host.clearTimeout(timer); }};
  }

  if (typeof module !== 'undefined' && module.exports) module.exports = {createSessionBanner, words};
  if (host.document) {
    // Started by app.js after its session bootstrap, so this never asks for a
    // token of its own or retries a failed startup.
    host.nexusSessionHealth = {start() {
      if (host.nexusSessionBanner || typeof host.request !== 'function') return host.nexusSessionBanner;
      host.nexusSessionBanner = createSessionBanner(host.document, (path, options) => host.request(path, options));
      return host.nexusSessionBanner;
    }};
    if (host.nexusSessionBannerWanted) host.nexusSessionHealth.start();
  }
})(typeof window !== 'undefined' ? window : globalThis);
