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

  function createLiveTeam(root, api) {
    const state = {agents: [], saved: [], running: [], team: null, teamId: '', after: 0, timer: null,
      bubbles: new Map(), tools: new Map(), questionsShown: new Set(), busy: false};
    root.replaceChildren();
    const header = el('header', undefined, 'lt-header');
    header.append(el('h2', 'Live team'), el('p', 'Agents keep one live session each, work at the same time and message each other. Everything they think, run and change appears here as it happens.', 'lt-lead'));
    const layout = el('div', undefined, 'lt-layout');
    const side = el('aside', undefined, 'lt-side');
    const main = el('section', undefined, 'lt-main');
    const board = el('aside', undefined, 'lt-board');
    layout.append(side, main, board);
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
    const start = el('button', 'Start team', 'primary'); start.type = 'submit'; start.id = 'ltStart';
    const formNote = el('p', '', 'lt-note');
    form.append(el('h3', 'Start a team'), agentList, field('Lead', lead), field('Project folder', project), field('Access', access), field('Working copies', mode), field('Goal', goal), start, formNote);
    const savedBox = el('div', undefined, 'lt-saved'); savedBox.append(el('h3', 'Teams'));
    const savedList = el('ul'); savedBox.append(savedList);
    side.append(form, savedBox);

    // -- main: status, timeline, composer
    const status = el('div', undefined, 'lt-status');
    const timeline = el('ol', undefined, 'lt-timeline'); timeline.setAttribute('aria-live', 'polite'); timeline.setAttribute('aria-label', 'Live team activity');
    const empty = el('p', 'Start a team or reopen one to see it work.', 'lt-empty');
    const composer = el('form', undefined, 'lt-composer');
    const to = el('select'); to.id = 'ltTo';
    const text = el('textarea'); text.id = 'ltText'; text.rows = 2; text.placeholder = 'Message the team…'; text.setAttribute('aria-label', 'Message the team');
    const send = el('button', 'Send', 'primary'); send.type = 'submit'; send.id = 'ltSend';
    const controls = el('div', undefined, 'lt-controls');
    controls.append(button('Interrupt', () => act({action: 'interrupt', team: state.teamId}), ''), button('Close team', () => { if (host.confirm('Close this team? Its sessions stop; you can reopen it later.')) act({action: 'close', team: state.teamId}); }, ''));
    composer.append(to, text, send);
    main.append(status, empty, timeline, composer, controls);

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
        const result = await act({action: 'create', agents: chosen, lead: lead.value || chosen[0], goal: goal.value, project: project.value, access: access.value, mode: mode.value});
        note('Team started.');
        openTeam(result.team.team_id);
        await refreshLists();
      } catch (_) { /* note shows the reason */ } finally { start.disabled = false; }
    });

    composer.addEventListener('submit', async event => {
      event.preventDefault();
      const words = text.value.trim();
      if (!words || !state.teamId) return;
      text.value = '';
      try { await act({action: 'say', team: state.teamId, text: words, to: to.value}); } catch (_) { text.value = words; }
    });
    text.addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); composer.requestSubmit(); } });

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
        const item = el('li', undefined, team.team_id === state.teamId ? 'lt-current' : '');
        item.append(el('strong', team.name || 'Team'), el('small', `${(team.agents || []).join(', ')} · ${team.state}`));
        item.append(button(team.state === 'running' || team.state === 'starting' ? 'Open' : 'Reopen', async () => {
          if (!(team.state === 'running' || team.state === 'starting')) { try { await act({action: 'reopen', team: team.team_id}); } catch (_) { return; } }
          openTeam(team.team_id);
        }, 'lt-small'));
        savedList.append(item);
      }
    }

    async function refreshLists() {
      try {
        const view = await api('/api/live-team');
        state.agents = view.agents || []; state.saved = view.saved || []; state.running = view.running || [];
        if (!project.value) project.value = view.project || '';
        renderAgentsForm(); renderSaved();
      } catch (error) { note(error.message, true); }
    }

    function resetTimeline() {
      state.after = 0; state.run = ''; state.bubbles.clear(); state.tools.clear(); state.questionsShown.clear();
      timeline.replaceChildren();
    }

    function openTeam(teamId) {
      if (state.teamId !== teamId) {
        state.teamId = teamId; resetTimeline();
      }
      renderSaved();
      poll();
    }

    function agentName(id) { return state.team?.agents?.find(a => a.id === id)?.name || id || 'Nexus'; }

    function renderStatus(team) {
      status.replaceChildren();
      empty.hidden = true;
      const head = el('div', undefined, 'lt-status-head');
      head.append(el('strong', team.name), el('small', `${team.state} · ${team.access.replace('_', ' ')} · ${team.mode === 'worktrees' ? 'separate worktrees' : 'shared folder'} · ${team.project}`));
      status.append(head);
      const chips = el('div', undefined, 'lt-chips');
      for (const agent of team.agents) {
        const session = agent.session || {};
        const chip = el('span', undefined, `lt-chip lt-${session.busy ? 'busy' : session.state || 'starting'}`);
        chip.title = `${agent.seat}\nsession ${session.session_id || '—'}${session.resume ? `\nresume: ${session.resume}` : ''}${session.error ? `\n${session.error}` : ''}`;
        chip.append(el('span', session.busy ? '●' : session.state === 'ready' ? '○' : session.state === 'error' ? '✗' : '…', 'lt-dot'),
          el('span', `${agent.name}${agent.id === team.lead ? ' (lead)' : ''}`),
          el('small', session.busy ? 'working' : session.state === 'ready' ? 'idle' : session.state || 'starting'));
        if (session.resume) chip.append(el('small', session.resume === 'resumed' ? 'conversation resumed' : 'started fresh', 'lt-resume'));
        if (session.queued) chip.append(el('small', `${session.queued} waiting`));
        chips.append(chip);
      }
      status.append(chips);
      const keep = to.value; to.replaceChildren();
      const everyone = el('option', 'Everyone'); everyone.value = ''; to.append(everyone);
      for (const agent of team.agents) { const o = el('option', agent.name); o.value = agent.id; to.append(o); }
      to.value = [...to.options].some(o => o.value === keep) ? keep : '';
    }

    function renderBoard(team) {
      board.replaceChildren();
      const structure = el('section', undefined, 'lt-structure'); structure.append(el('h3', 'Team'));
      const counts = new Map();
      for (const message of team.messages || []) { const key = `${message.sender}→${message.recipient}`; counts.set(key, (counts.get(key) || 0) + 1); }
      const roster = el('ul');
      for (const agent of team.agents) roster.append(el('li', `${agent.name}${agent.id === team.lead ? ' · lead' : ''} · ${agent.seat}${agent.branch ? ` · ${agent.branch}` : ''}`));
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
        if (!bubble) { bubble = {item: row('lt-message', event.agent), body: el('p'), text: ''}; bubble.item.append(bubble.body); state.bubbles.set(key, bubble); }
        bubble.text = event.delta ? bubble.text + (event.text || '') : (event.text || '');
        bubble.body.textContent = bubble.text;
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
          const answer = el('input'); answer.placeholder = 'Your answer'; const wrap = el('div', undefined, 'lt-answer');
          wrap.append(answer, button('Answer', async () => { if (!answer.value.trim()) return; await act({action: 'answer', team: state.teamId, question: event.question, answer: answer.value}); wrap.replaceChildren(el('small', `Answered: ${answer.value}`)); }, 'primary'));
          item.append(wrap); return;
        }
        if (event.level === 'delivery') { const item = row('lt-delivery', event.agent); item.append(el('span', `→ ${event.to === '*' ? 'everyone' : agentName(event.to)}`, 'lt-to'), el('p', event.text)); return; }
        if (event.level === 'result') { const item = row(`lt-result lt-result-${event.status}`, event.agent); item.append(el('strong', event.status === 'done' ? 'Done' : event.status === 'blocked' ? 'Blocked' : 'Needs your input'), el('p', event.text)); return; }
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

    async function poll() {
      host.clearTimeout(state.timer);
      if (!state.teamId) return;
      const team = state.teamId;
      try {
        const view = await api(`/api/live-team/team?team=${encodeURIComponent(team)}&after=${state.after}`);
        if (state.teamId !== team) return;
        // A reopened team is a new run that numbers its events from the start again.
        if (state.run && view.team?.run && view.team.run !== state.run) { resetTimeline(); host.setTimeout(poll, 0); return; }
        state.run = view.team?.run || state.run;
        state.team = view.team;
        const nearBottom = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 80;
        for (const event of view.events || []) { state.after = Math.max(state.after, event.seq); addEvent(event); }
        if (nearBottom) timeline.scrollTop = timeline.scrollHeight;
        renderStatus(view.team); renderBoard(view.team);
      } catch (error) { status.replaceChildren(el('p', error.message, 'lt-bad')); }
      if (state.teamId === team && root.isConnected) state.timer = host.setTimeout(poll, POLL_MS);
    }

    refreshLists().then(() => { const live = state.running[0]; if (live && !state.teamId) openTeam(live.team_id); });
    return {refresh: refreshLists, open: openTeam, state};
  }

  if (typeof module !== 'undefined' && module.exports) module.exports = {createLiveTeam};
  if (host.document) {
    let view = null;
    host.nexusLiveTeam = {refresh() {
      const root = host.document.getElementById('liveTeamView');
      if (!root || typeof host.request !== 'function') return;
      if (!view) view = createLiveTeam(root, (path, options) => host.request(path, options)); else view.refresh();
    }};
  }
})(typeof window !== 'undefined' ? window : globalThis);
