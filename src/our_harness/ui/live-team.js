// "Live team" tab: Agent Runtime v3. Persistent agent sessions work in
// parallel, message each other through Nexus, and everything they do streams
// here: text as it is written, thinking, every command and edit, the shared
// task board, teammate messages, questions and approvals.
(function (host) {
  'use strict';
  const POLL_MS = 700;
  const ICONS = {running: '▶', finished: '✓', failed: '✗', requested: '▶'};

  function el(tag, text, cls) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function button(label, onClick, cls) {
    const node = el('button', label, cls); node.type = 'button'; node.addEventListener('click', onClick); return node;
  }
  function field(labelText, control) {
    const wrap = el('label', undefined, 'lt-field'); wrap.append(el('span', labelText), control); return wrap;
  }

  // Agents write Markdown. Shown as formatted text built from DOM nodes (never
  // innerHTML), so nothing an agent writes can run in the page.
  const INLINE = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*|__[^_\n]+__)|(\*[^*\s\n][^*\n]*\*|\b_[^_\n]+_\b)|(\[[^\]\n]+\]\((?:https?:\/\/|file:\/\/)[^)\s]+\))/g;
  function inline(parent, text) {
    let last = 0;
    for (const match of String(text).matchAll(INLINE)) {
      if (match.index > last) parent.append(String(text).slice(last, match.index));
      const [token, code, bold, italic, link] = match;
      if (code) parent.append(el('code', token.slice(1, -1)));
      else if (bold) { const node = el('strong'); inline(node, token.slice(2, -2)); parent.append(node); }
      else if (italic) { const node = el('em'); inline(node, token.slice(1, -1)); parent.append(node); }
      else if (link) {
        const split = token.indexOf('](');
        const anchor = el('a', token.slice(1, split)); anchor.href = token.slice(split + 2, -1);
        anchor.target = '_blank'; anchor.rel = 'noopener noreferrer'; parent.append(anchor);
      }
      last = match.index + token.length;
    }
    if (last < String(text).length) parent.append(String(text).slice(last));
  }
  function renderMarkdown(target, text) {
    target.replaceChildren();
    const lines = String(text || '').replace(/\r\n?/g, '\n').split('\n');
    let paragraph = null, list = null, code = null;
    const close = () => { paragraph = null; list = null; };
    for (const line of lines) {
      if (code) {
        if (/^\s*```/.test(line)) { code = null; continue; }
        code.textContent += (code.textContent ? '\n' : '') + line; continue;
      }
      if (/^\s*```/.test(line)) { close(); const pre = el('pre', undefined, 'lt-md-code'); code = el('code'); pre.append(code); target.append(pre); continue; }
      if (!line.trim()) { close(); continue; }
      const heading = /^\s{0,3}(#{1,6})\s+(.*)$/.exec(line);
      if (heading) { close(); const node = el('div', undefined, `lt-md-h lt-md-h${Math.min(heading[1].length, 4)}`); inline(node, heading[2].replace(/\s#+\s*$/, '')); target.append(node); continue; }
      if (/^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(line)) { close(); target.append(el('hr')); continue; }
      const item = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/.exec(line);
      if (item) {
        const ordered = /\d/.test(item[2]);
        if (!list || list.ordered !== ordered) { paragraph = null; list = {ordered, node: el(ordered ? 'ol' : 'ul', undefined, 'lt-md-list')}; target.append(list.node); }
        const li = el('li'); if (item[1].length >= 2) li.className = 'lt-md-nested'; inline(li, item[3]); list.node.append(li); continue;
      }
      const quote = /^\s*>\s?(.*)$/.exec(line);
      if (quote) { close(); const node = el('blockquote'); inline(node, quote[1]); target.append(node); continue; }
      if (list && /^\s{2,}\S/.test(line)) { const li = list.node.lastElementChild; li.append(' '); inline(li, line.trim()); continue; }
      list = null;
      if (!paragraph) { paragraph = el('p'); target.append(paragraph); } else paragraph.append(el('br'));
      inline(paragraph, line);
    }
  }

  // Files the user adds to a message, read in the page and sent with it.
  const MOST_FILES = 10, MOST_BYTES = 8000000;
  function readFile(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve({name: file.name || 'pasted file', type: file.type || '', size: file.size, data: String(reader.result || '')});
      reader.onerror = () => reject(new Error(`Could not read ${file.name || 'the file'}.`));
      reader.readAsDataURL(file);
    });
  }
  // The command the agent meant, without the shell wrapper its CLI adds.
  function readableCommand(command) {
    let inner = String(command || '').replace(/^\s*"?[^"\s]*(?:powershell|pwsh|cmd|bash|sh)(?:\.exe)?"?\s+(?:-NoProfile\s+)?(?:-Command|-c|\/c)\s+/i, '').trim();
    if (inner.length >= 2 && inner[0] === inner[inner.length - 1] && `'"`.includes(inner[0])) inner = inner.slice(1, -1);
    return inner || String(command || '');
  }

  // What an approval request asks for, in plain words.
  function describeApproval(text) {
    let value = null;
    try { value = JSON.parse(text); } catch (_) { return {title: 'Wants your approval', detail: String(text || '')}; }
    if (value.tool && value.input !== undefined) {
      const input = value.input || {};
      const what = input.command ? readableCommand(input.command) : input.file_path || input.path || JSON.stringify(input);
      return {title: `Wants to use ${String(value.tool).replace(/^mcp__[^_]+__/, '')}`, detail: String(what)};
    }
    if (value.command) {
      return {title: 'Wants to run a command', detail: readableCommand(value.command),
        where: value.cwd ? `in ${value.cwd}` : '', reason: value.reason || ''};
    }
    if (value.changes) {
      const files = (Array.isArray(value.changes) ? value.changes : Object.keys(value.changes || {})).map(one => typeof one === 'string' ? one : one.path).filter(Boolean);
      return {title: 'Wants to change files', detail: files.join(', ') || 'files in the project', reason: value.reason || ''};
    }
    if (value.server || value.message) return {title: `Wants to use a tool from ${value.server || 'a tool server'}`, detail: value.message || ''};
    if (value.title) return {title: 'Wants your approval', detail: value.title};
    return {title: 'Wants your approval', detail: JSON.stringify(value)};
  }

  // One team's live view: status chips, the streaming timeline, the composer
  // and the task board. The Live team tab and every board chat that runs as a
  // live team use it. `options.start(words, to, state)` runs when there is no
  // running team yet and resolves to the id of the team to show.
  function createTeamView(api, options = {}) {
    const prefix = options.idPrefix || 'lt';
    const visible = options.visible || (() => true);
    const state = {team: null, teamId: '', after: 0, run: '', timer: null,
      bubbles: new Map(), tools: new Map(), questionsShown: new Set()};
    const main = el('section', undefined, 'lt-main');
    const board = el('aside', undefined, 'lt-board');
    const status = el('div', undefined, 'lt-status');
    const timeline = el('ol', undefined, 'lt-timeline'); timeline.setAttribute('aria-live', 'polite'); timeline.setAttribute('aria-label', 'Live team activity');
    const empty = el('p', options.emptyText || 'Start a team or reopen one to see it work.', 'lt-empty');
    const composer = el('form', undefined, 'lt-composer');
    const to = el('select'); to.id = `${prefix}To`; to.setAttribute('aria-label', 'Send to');
    const text = el('textarea'); text.id = `${prefix}Text`; text.rows = 2; text.placeholder = options.placeholder || 'Message the team…'; text.setAttribute('aria-label', 'Message the team');
    const send = el('button', 'Send', 'primary'); send.type = 'submit'; send.id = `${prefix}Send`;
    const picker = el('input'); picker.type = 'file'; picker.multiple = true; picker.hidden = true; picker.id = `${prefix}Files`;
    const attach = button('Attach', () => picker.click(), 'lt-attach'); attach.id = `${prefix}Attach`;
    attach.title = 'Attach pictures or files for the agents (you can also paste or drop them here)';
    attach.setAttribute('aria-label', 'Attach files');
    const chips = el('div', undefined, 'lt-attachments'); chips.hidden = true;
    const note = el('p', '', 'lt-note'); note.setAttribute('role', 'status');
    const browserChoice = el('select'); browserChoice.id = `${prefix}Browser`; browserChoice.setAttribute('aria-label', 'Browser checks');
    for (const [value, label] of [['hidden', 'Hidden'], ['visible', 'Visible']]) { const o = el('option', label); o.value = value; browserChoice.append(o); }
    browserChoice.title = 'Hidden: agents look at pages in a browser you never see, and no tabs or windows open on your screen. Visible: you see the browser while they check.';
    const browserField = el('label', undefined, 'lt-browser'); browserField.append(el('span', 'Browser checks'), browserChoice);
    const controls = el('div', undefined, 'lt-controls');
    const interrupt = button('Interrupt', () => act({action: 'interrupt', team: state.teamId}), '');
    const close = button('Close team', () => {
      if (host.confirm('Close this team? Its sessions stop; you can reopen it later.')) act({action: 'close', team: state.teamId}).then(() => poll(), () => {});
    }, '');
    controls.append(interrupt, close);
    const bottom = el('div', undefined, 'lt-bottom');
    bottom.append(browserField, controls);
    composer.append(to, attach, text, send, picker);
    main.append(status, empty, timeline, chips, composer, note, bottom);
    renderRecipients([]);
    const files = [];

    function renderChips() {
      chips.replaceChildren();
      chips.hidden = !files.length;
      files.forEach((one, index) => {
        const chip = el('span', undefined, 'lt-attachment');
        chip.append(el('span', `${one.name} · ${Math.max(1, Math.round(one.size / 1024))} KB`));
        const remove = button('×', () => { files.splice(index, 1); renderChips(); }, 'lt-attachment-remove');
        remove.setAttribute('aria-label', `Remove ${one.name}`);
        chip.append(remove); chips.append(chip);
      });
    }
    async function addFiles(list) {
      const chosen = [...(list || [])];
      if (!chosen.length) return;
      try {
        if (files.length + chosen.length > MOST_FILES) throw new Error(`Attach at most ${MOST_FILES} files at once.`);
        const read = await Promise.all(chosen.map(readFile));
        if ([...files, ...read].reduce((sum, one) => sum + one.size, 0) > MOST_BYTES) throw new Error('The attachments together are larger than 8 MB.');
        files.push(...read); renderChips(); say('');
      } catch (error) { say(error.message, true); }
    }
    picker.addEventListener('change', () => { void addFiles(picker.files); picker.value = ''; });
    text.addEventListener('paste', event => {
      const pasted = [...(event.clipboardData?.files || [])];
      if (!pasted.length) return; // Ordinary text paste keeps the browser's behaviour.
      event.preventDefault(); void addFiles(pasted);
    });
    composer.addEventListener('dragover', event => { if ([...(event.dataTransfer?.types || [])].includes('Files')) { event.preventDefault(); composer.classList.add('lt-drop'); } });
    composer.addEventListener('dragleave', () => composer.classList.remove('lt-drop'));
    composer.addEventListener('drop', event => {
      composer.classList.remove('lt-drop');
      if (!event.dataTransfer?.files?.length) return;
      event.preventDefault(); void addFiles(event.dataTransfer.files);
    });

    let browserKnown = false;
    function showBrowser(mode) { browserKnown = true; browserChoice.value = mode === 'visible' ? 'visible' : 'hidden'; }
    browserChoice.addEventListener('change', async () => {
      const mode = browserChoice.value;
      try {
        await act({action: 'browser', mode, team: running() ? state.teamId : ''});
        say(mode === 'hidden' ? 'Browser checks are hidden: nothing opens on your screen.' : 'Browser checks are visible: you will see the browser while agents check pages.');
      } catch (_) { showBrowser(mode === 'hidden' ? 'visible' : 'hidden'); }
    });
    api('/api/live-team').then(listed => { if (!browserKnown) showBrowser(listed?.settings?.browser); }, () => {});

    function say(message, bad) { note.textContent = message || ''; note.classList.toggle('lt-bad', !!bad); }
    async function act(body) {
      try { return await api('/api/live-team', {method: 'POST', body: JSON.stringify(body)}); }
      catch (error) { say(error.message, true); throw error; }
    }
    const running = () => ['running', 'starting'].includes(state.team?.state);

    composer.addEventListener('submit', async event => {
      event.preventDefault();
      const words = text.value.trim();
      if ((!words && !files.length) || send.disabled) return;
      const sending = files.splice(0);
      text.value = ''; send.disabled = true; renderChips();
      try {
        if (options.start && (!state.teamId || !running())) {
          say(state.teamId ? 'Reopening the team…' : 'Starting the agents…');
          const teamId = await options.start(words, to.value, state, sending);
          say('');
          if (teamId) open(teamId);
        } else if (state.teamId) {
          await act({action: 'say', team: state.teamId, text: words, to: to.value, ...(sending.length ? {attachments: sending} : {})});
        } else { text.value = words; files.push(...sending); renderChips(); }
      } catch (error) { text.value = words; files.unshift(...sending); renderChips(); say(error.message, true); }
      finally { send.disabled = false; }
    });
    text.addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); composer.requestSubmit(); } });

    function renderRecipients(agents) {
      const keep = to.value; to.replaceChildren();
      const everyone = el('option', 'Everyone'); everyone.value = ''; to.append(everyone);
      for (const agent of agents) { const o = el('option', agent.name); o.value = agent.id; to.append(o); }
      to.value = [...to.options].some(o => o.value === keep) ? keep : '';
    }

    function reset() {
      state.after = 0; state.run = ''; state.bubbles.clear(); state.tools.clear(); state.questionsShown.clear();
      timeline.replaceChildren();
    }

    function open(teamId) {
      if (state.teamId !== (teamId || '')) { state.teamId = teamId || ''; state.team = null; reset(); }
      if (!state.teamId) {
        host.clearTimeout(state.timer);
        status.replaceChildren(); board.replaceChildren(); renderRecipients([]);
        empty.hidden = false; controls.hidden = true;
        return;
      }
      poll();
    }

    function agentName(id) { return state.team?.agents?.find(a => a.id === id)?.name || id || 'Nexus'; }

    function renderStatus(team) {
      status.replaceChildren();
      empty.hidden = true;
      controls.hidden = false;
      interrupt.disabled = close.disabled = !running();
      if (team.browser) showBrowser(team.browser);
      const head = el('div', undefined, 'lt-status-head');
      const where = [team.state, team.access ? String(team.access).replace('_', ' ') : '', team.mode ? (team.mode === 'worktrees' ? 'separate worktrees' : 'shared folder') : '', team.project].filter(Boolean);
      head.append(el('strong', team.name), el('small', where.join(' · ')));
      status.append(head);
      const chips = el('div', undefined, 'lt-chips');
      for (const agent of team.agents || []) {
        const session = agent.session || {};
        const chip = el('span', undefined, `lt-chip lt-${session.busy ? 'busy' : session.state || 'starting'}`);
        chip.title = `${agent.seat || agent.name}\nsession ${session.session_id || '—'}${session.resume ? `\nresume: ${session.resume}` : ''}${session.error ? `\n${session.error}` : ''}`;
        const idle = !running() ? 'not running' : session.state || 'starting';
        chip.append(el('span', session.busy ? '●' : session.state === 'ready' ? '○' : session.state === 'error' ? '✗' : '…', 'lt-dot'),
          el('span', `${agent.name}${agent.id === team.lead ? ' (lead)' : ''}`),
          el('small', session.busy ? 'working' : session.state === 'ready' ? 'idle' : idle));
        if (session.resume) chip.append(el('small', session.resume === 'resumed' ? 'conversation resumed' : 'started fresh', 'lt-resume'));
        if (session.queued) chip.append(el('small', `${session.queued} waiting`));
        const used = agent.usage || {};
        if (used.input_tokens || used.output_tokens) {
          const tokens = n => n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}K` : String(n);
          chip.append(el('small', `${tokens(used.input_tokens || 0)} in · ${tokens(used.output_tokens || 0)} out`, 'lt-usage'));
        }
        chips.append(chip);
      }
      status.append(chips);
      if (!running() && options.start) status.append(el('p', 'This team is not running. Your next message reopens it and carries on from its saved conversation.', 'lt-notice'));
      renderRecipients(team.agents || []);
    }

    function renderBoard(team) {
      board.replaceChildren();
      const structure = el('section', undefined, 'lt-structure'); structure.append(el('h3', 'Team'));
      const counts = new Map();
      for (const message of team.messages || []) { const key = `${message.sender}→${message.recipient}`; counts.set(key, (counts.get(key) || 0) + 1); }
      const roster = el('ul');
      for (const agent of team.agents || []) roster.append(el('li', `${agent.name}${agent.id === team.lead ? ' · lead' : ''}${agent.seat ? ` · ${agent.seat}` : ''}${agent.branch ? ` · ${agent.branch}` : ''}`));
      structure.append(roster);
      if (counts.size) { const lines = el('ul', undefined, 'lt-lines'); for (const [key, count] of counts) { const [from, dest] = key.split('→'); lines.append(el('li', `${agentName(from)} → ${dest === '*' ? 'everyone' : agentName(dest)}: ${count}`)); } structure.append(el('h4', 'Messages'), lines); }
      board.append(structure);
      const tasks = el('section', undefined, 'lt-tasks'); tasks.append(el('h3', 'Tasks'));
      const list = el('ul');
      for (const task of team.tasks || []) {
        const item = el('li', undefined, `lt-task lt-task-${task.state}`);
        item.append(el('strong', task.title), el('small', `${task.state.replace('_', ' ')}${task.owner ? ` · ${agentName(task.owner)}` : ''}${task.closure_reason ? ` · ${task.closure_reason.replace('_', ' ')}${task.closure_target ? ` → ${agentName(task.closure_target)}` : ''}` : ''}`));
        list.append(item);
      }
      if (!(team.tasks || []).length) list.append(el('li', 'No tasks yet.', 'lt-muted'));
      tasks.append(list); board.append(tasks);
      if ((team.leases || []).length) { const leases = el('section', undefined, 'lt-leases'); leases.append(el('h3', 'Files being edited')); const l = el('ul'); for (const lease of team.leases) l.append(el('li', `${lease.pattern} · ${agentName(lease.holder)}`)); leases.append(l); board.append(leases); }
    }

    function row(cls, agent) {
      const item = el('li', undefined, `lt-row ${cls}`);
      if (agent) item.append(el('span', agentName(agent), 'lt-who'));
      timeline.append(item);
      return item;
    }

    function addEvent(event) {
      const kind = event.kind;
      if (kind === 'message') {
        if (event.role === 'user') { const item = row('lt-user', ''); item.append(el('span', `You → ${agentName(event.agent)}`, 'lt-who'), el('p', event.text)); return; }
        if (event.role === 'briefing') {
          const item = row('lt-briefing', ''); const details = el('details');
          details.append(el('summary', `Nexus briefed ${agentName(event.agent)}`), el('p', event.text)); item.append(details); return;
        }
        const key = `m:${event.agent}`;
        let bubble = state.bubbles.get(key);
        if (!bubble) { bubble = {item: row('lt-message', event.agent), body: el('div', undefined, 'lt-md'), text: ''}; bubble.item.append(bubble.body); state.bubbles.set(key, bubble); }
        bubble.text = event.delta ? bubble.text + (event.text || '') : (event.text || '');
        renderMarkdown(bubble.body, bubble.text);
        if (!event.delta) state.bubbles.delete(key);
        return;
      }
      if (kind === 'thought') {
        const key = `t:${event.agent}`;
        let bubble = state.bubbles.get(key);
        if (!bubble && !String(event.text || '').trim()) return;
        if (!bubble) {
          const item = row('lt-thought', event.agent); const details = el('details'); details.open = true;
          const summary = el('summary', 'Thinking'); const body = el('p'); details.append(summary, body); item.append(details);
          bubble = {item, body, text: ''}; state.bubbles.set(key, bubble);
        }
        bubble.text = event.delta ? bubble.text + (event.text || '') : (event.text || '');
        bubble.body.textContent = bubble.text;
        if (!event.delta) state.bubbles.delete(key);
        return;
      }
      if (kind === 'turn' && event.status !== 'started') {
        state.bubbles.delete(`m:${event.agent}`); state.bubbles.delete(`t:${event.agent}`);
        if (event.status === 'failed') { const item = row('lt-error', event.agent); item.append(el('p', `Turn failed: ${event.text || 'no reason given'}`)); }
        if (event.status === 'interrupted') row('lt-notice', event.agent).append(el('p', 'Stopped. Send a message to carry on.'));
        return;
      }
      if (kind === 'tool') {
        const key = `${event.agent}:${event.id}`;
        let entry = state.tools.get(key);
        if (!entry) {
          const item = row('lt-tool', event.agent); const details = el('details'); const summary = el('summary');
          const icon = el('span', '', 'lt-icon'); const title = el('span', '', 'lt-tool-title'); summary.append(icon, title);
          const output = el('pre', '', 'lt-output'); details.append(summary, output); item.append(details);
          entry = {item, icon, title, output}; state.tools.set(key, entry);
        }
        entry.icon.textContent = ICONS[event.status] || '•';
        entry.title.textContent = event.title || event.name;
        entry.item.dataset.status = event.status;
        if (event.output) entry.output.textContent = event.output;
        return;
      }
      if (kind === 'permission') {
        if (state.questionsShown.has(event.question)) return; state.questionsShown.add(event.question);
        const described = describeApproval(event.text);
        const item = row('lt-ask', event.agent); item.append(el('p', described.title + (described.where ? ` ${described.where}` : '') + ':'), el('pre', described.detail, 'lt-output'));
        if (described.reason) item.append(el('small', `Reason given: ${described.reason}`));
        const buttons = el('div', undefined, 'lt-answer');
        for (const [label, value] of [['Allow', 'accept'], ['Allow for this session', 'acceptForSession'], ['Deny', 'decline']]) buttons.append(button(label, async () => { await act({action: 'answer', team: state.teamId, question: event.question, answer: value}); buttons.replaceChildren(el('small', `Answered: ${label}`)); }, value === 'decline' ? '' : 'primary'));
        item.append(buttons); return;
      }
      if (kind === 'notice') {
        if (event.level === 'question') {
          if (state.questionsShown.has(event.question)) return; state.questionsShown.add(event.question);
          const item = row('lt-ask', event.agent); item.append(el('p', event.text));
          const answer = el('input'); answer.placeholder = 'Your answer'; answer.setAttribute('aria-label', 'Your answer'); const wrap = el('div', undefined, 'lt-answer');
          wrap.append(answer, button('Answer', async () => { if (!answer.value.trim()) return; await act({action: 'answer', team: state.teamId, question: event.question, answer: answer.value}); wrap.replaceChildren(el('small', `Answered: ${answer.value}`)); }, 'primary'));
          item.append(wrap); return;
        }
        if (event.level === 'delivery') { const item = row('lt-delivery', event.agent); const body = el('div', undefined, 'lt-md'); renderMarkdown(body, event.text); item.append(el('span', `→ ${event.to === '*' ? 'everyone' : agentName(event.to)}`, 'lt-to'), body); return; }
        if (event.level === 'result') { const item = row(`lt-result lt-result-${event.status}`, event.agent); const body = el('div', undefined, 'lt-md'); renderMarkdown(body, event.text); item.append(el('strong', event.status === 'done' ? 'Done' : event.status === 'blocked' ? 'Blocked' : 'Needs your input'), body); return; }
        if (event.level === 'answered') return;
        const item = row(`lt-notice lt-${event.level || 'info'}`, event.agent); item.append(el('p', event.text)); return;
      }
      if (kind === 'session' && (event.state === 'error' || event.resume)) {
        const item = row(event.state === 'error' ? 'lt-error' : 'lt-notice', event.agent);
        item.append(el('p', event.state === 'error' ? `Session problem: ${event.text || 'it stopped'}` : event.resume === 'resumed' ? 'Picked up the saved conversation.' : 'Could not resume the saved conversation; started a fresh one.'));
      }
      if (kind === 'plan' && (event.entries || []).length) {
        const item = row('lt-plan', event.agent); item.append(el('strong', 'Plan'));
        const list = el('ul'); for (const step of event.entries) list.append(el('li', `${step.status === 'completed' ? '✓' : step.status === 'in_progress' ? '▶' : '○'} ${step.step || step.content || step.title || ''}`)); item.append(list);
      }
    }

    function show(team) {
      state.team = team;
      renderStatus(team); renderBoard(team);
      options.onTeam?.(team);
    }

    async function poll() {
      host.clearTimeout(state.timer);
      if (!state.teamId) return;
      const team = state.teamId;
      // A detached view restarts when it is shown again; a hidden one
      // (a minimised window, another mode) waits without fetching.
      if (!main.isConnected) return;
      if (!visible()) { state.timer = host.setTimeout(poll, 2000); return; }
      try {
        const view = await api(`/api/live-team/team?team=${encodeURIComponent(team)}&after=${state.after}`);
        if (state.teamId !== team) return;
        // A reopened team is a new run that numbers its events from the start again.
        if (state.run && view.team?.run && view.team.run !== state.run) { reset(); host.setTimeout(poll, 0); return; }
        state.run = view.team?.run || state.run;
        state.team = view.team;
        const nearBottom = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 80;
        for (const event of view.events || []) { state.after = Math.max(state.after, event.seq); addEvent(event); }
        if (nearBottom) timeline.scrollTop = timeline.scrollHeight;
        show(view.team);
      } catch (error) {
        if (state.teamId !== team) return;
        // A saved team that is not running has no live events; show what is saved.
        const saved = options.saved?.(team);
        if (saved) show(saved); else status.replaceChildren(el('p', error.message, 'lt-bad'));
      }
      if (state.teamId === team && main.isConnected) state.timer = host.setTimeout(poll, running() ? POLL_MS : POLL_MS * 4);
    }

    function stop() { host.clearTimeout(state.timer); }
    return {main, board, open, poll, stop, state, act, say, text, addFiles};
  }

  function createLiveTeam(root, api) {
    const state = {agents: [], saved: [], running: []};
    root.replaceChildren();
    const header = el('header', undefined, 'lt-header');
    header.append(el('h2', 'Live team'), el('p', 'Agents keep one live session each, work at the same time and message each other. Everything they think, run and change appears here as it happens.', 'lt-lead'));
    const layout = el('div', undefined, 'lt-layout');
    const side = el('aside', undefined, 'lt-side');
    const view = createTeamView(api);
    layout.append(side, view.main, view.board);
    root.append(header, layout);

    // -- start form
    const form = el('form', undefined, 'lt-form'); form.setAttribute('aria-label', 'Start a live team');
    const agentList = el('fieldset', undefined, 'lt-agents'); agentList.append(el('legend', 'Agents'));
    const lead = el('select'); lead.id = 'ltLead';
    const project = el('input'); project.id = 'ltProject'; project.placeholder = 'Project folder';
    const access = el('select'); access.id = 'ltAccess';
    for (const [value, text] of [['full', 'Full project access'], ['ask', 'Ask before commands'], ['read_only', 'Read only']]) { const o = el('option', text); o.value = value; access.append(o); }
    const mode = el('select'); mode.id = 'ltMode';
    for (const [value, text] of [['shared', 'Shared project folder'], ['worktrees', 'Separate git worktree per agent']]) { const o = el('option', text); o.value = value; mode.append(o); }
    const goal = el('textarea'); goal.id = 'ltGoal'; goal.rows = 4; goal.placeholder = 'What should the team do?';
    const browser = el('select'); browser.id = 'ltStartBrowser';
    for (const [value, text] of [['hidden', 'Hidden (nothing opens on your screen)'], ['visible', 'Visible']]) { const o = el('option', text); o.value = value; browser.append(o); }
    browser.addEventListener('change', () => { browser.dataset.touched = '1'; });
    const start = el('button', 'Start team', 'primary'); start.type = 'submit'; start.id = 'ltStart';
    const formNote = el('p', '', 'lt-note');
    form.append(el('h3', 'Start a team'), agentList, field('Lead', lead), field('Project folder', project), field('Access', access), field('Working copies', mode), field('Browser checks', browser), field('Goal', goal), start, formNote);
    const savedBox = el('div', undefined, 'lt-saved'); savedBox.append(el('h3', 'Teams'));
    const savedList = el('ul'); savedBox.append(savedList);
    side.append(form, savedBox);

    function note(message, bad) { formNote.textContent = message || ''; formNote.classList.toggle('lt-bad', !!bad); }

    async function act(body) {
      try { return await api('/api/live-team', {method: 'POST', body: JSON.stringify(body)}); }
      catch (error) { note(error.message, true); throw error; }
    }

    form.addEventListener('submit', async event => {
      event.preventDefault();
      const chosen = [...agentList.querySelectorAll('input[type=checkbox]:checked')].map(box => box.value);
      if (!chosen.length) return note('Choose at least one agent.', true);
      if (!goal.value.trim()) return note('Describe what the team should do.', true);
      start.disabled = true; note('Starting the agents…');
      try {
        const result = await act({action: 'create', agents: chosen, lead: lead.value || chosen[0], goal: goal.value, project: project.value, access: access.value, mode: mode.value, browser: browser.value});
        note('Team started.');
        openTeam(result.team.team_id);
        await refreshLists();
      } catch (_) { /* note shows the reason */ } finally { start.disabled = false; }
    });

    function renderAgentsForm() {
      const keep = new Set([...agentList.querySelectorAll('input:checked')].map(box => box.value));
      agentList.querySelectorAll('label').forEach(one => one.remove());
      for (const agent of state.agents) {
        const row = el('label', undefined, 'lt-agent-choice');
        const box = el('input'); box.type = 'checkbox'; box.value = agent.id; box.disabled = !agent.supported;
        box.setAttribute('aria-label', agent.name);
        box.checked = keep.has(agent.id);
        box.addEventListener('change', renderLead);
        row.append(box, el('span', agent.name), el('small', agent.supported ? `${agent.kind.replace('-cli', '')} · ${agent.model || 'default model'}` : `${agent.kind || agent.route || 'no connection'} · not supported yet`));
        agentList.append(row);
      }
      if (!state.agents.length) agentList.append(el('label', 'Add agents on the AI Agent Swarm board first.'));
      renderLead();
    }
    function renderLead() {
      const chosen = [...agentList.querySelectorAll('input:checked')].map(box => state.agents.find(a => a.id === box.value)).filter(Boolean);
      const current = lead.value; lead.replaceChildren();
      for (const agent of chosen) { const o = el('option', agent.name); o.value = agent.id; lead.append(o); }
      if (chosen.some(a => a.id === current)) lead.value = current;
    }
    function renderSaved() {
      savedList.replaceChildren();
      if (!state.saved.length) savedList.append(el('li', 'No teams yet.', 'lt-muted'));
      for (const team of state.saved) {
        const item = el('li', undefined, team.team_id === view.state.teamId ? 'lt-current' : '');
        item.append(el('strong', team.name || 'Team'), el('small', `${(team.agents || []).join(', ')} · ${team.state}${team.chat ? ' · board chat' : ''}`));
        item.append(button(team.state === 'running' || team.state === 'starting' ? 'Open' : 'Reopen', async () => {
          if (!(team.state === 'running' || team.state === 'starting')) { try { await act({action: 'reopen', team: team.team_id}); } catch (_) { return; } }
          openTeam(team.team_id);
        }, 'lt-small'));
        savedList.append(item);
      }
    }

    async function refreshLists() {
      try {
        const listed = await api('/api/live-team');
        state.agents = listed.agents || []; state.saved = listed.saved || []; state.running = listed.running || [];
        if (!project.value) project.value = listed.project || '';
        if (listed.settings?.browser && !browser.dataset.touched) browser.value = listed.settings.browser;
        renderAgentsForm(); renderSaved();
      } catch (error) { note(error.message, true); }
    }

    function openTeam(teamId) { view.open(teamId); renderSaved(); }

    refreshLists().then(() => { const live = state.running[0]; if (live && !view.state.teamId) openTeam(live.team_id); });
    return {refresh: refreshLists, open: openTeam, state, view};
  }

  if (typeof module !== 'undefined' && module.exports) module.exports = {createLiveTeam, createTeamView, renderMarkdown};
  if (host.document) {
    let view = null;
    host.nexusLiveTeam = {createTeamView, refresh() {
      const root = host.document.getElementById('liveTeamView');
      if (!root || typeof host.request !== 'function') return;
      if (!view) view = createLiveTeam(root, (path, options) => host.request(path, options)); else view.refresh();
    }};
  }
})(typeof window !== 'undefined' ? window : globalThis);
