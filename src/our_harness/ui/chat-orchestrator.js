// Each board chat picks its orchestrator. "Nexus orchestrator" is the saved
// Nexus conversation with Work together goals; "Live team" runs the chat's
// agents as a live team (Agent Runtime v3) with the streaming view from the
// Live team tab. Every chat keeps its own team, so many can run at once; the
// server remembers the choice and the team per chat.
(function (host) {
  'use strict';
  const MODES = [['nexus', 'Nexus orchestrator'], ['live_team', 'Live team']];
  const views = new Map();
  let modes = {};
  let listedAt = 0;
  let current = {agentId: '', chatId: ''};
  let changing = false;

  const $ = id => host.document.getElementById(id);
  const api = (path, options) => host.request(path, options);
  function el(tag, text, cls) {
    const node = host.document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function chatFacts(agentId, conversation) {
    const ids = (Array.isArray(conversation?.pair) && conversation.pair.length ? conversation.pair
      : (conversation?.pair_agents || []).map(one => one.id)).map(String).filter(Boolean);
    const names = new Map((conversation?.pair_agents || []).map(one => [String(one.id), one.name]));
    const project = (conversation?.projects || []).find(one => one.id === conversation?.project);
    let access = 'full';
    try { access = host.chatComposerAccessPreference?.(conversation) || 'full'; } catch (_) {}
    return {ids, names, project, access, lead: ids.includes(String(agentId)) ? String(agentId) : ids[0] || ''};
  }

  function ensureControls() {
    const bar = $('theBigChatOrchestrator');
    if (!bar || bar.childElementCount) return bar;
    bar.append(el('span', 'Orchestrator', 'chat-orchestrator-label'));
    for (const [mode, label] of MODES) {
      const choice = el('button', label, 'chat-orchestrator-choice');
      choice.type = 'button';
      choice.id = mode === 'nexus' ? 'theBigChatUseNexus' : 'theBigChatUseLiveTeam';
      choice.dataset.mode = mode;
      choice.setAttribute('aria-pressed', 'false');
      choice.title = mode === 'nexus'
        ? 'Nexus coordinates the agents: saved conversation, Work together goals, reviews and collaboration settings.'
        : 'The agents keep live sessions, work at the same time and message each other. Everything streams here.';
      choice.addEventListener('click', () => void choose(mode));
      bar.append(choice);
    }
    return bar;
  }

  function modeOf(chatId) { return modes[chatId]?.mode === 'live_team' ? 'live_team' : 'nexus'; }

  async function loadModes(force) {
    if (!force && Date.now() - listedAt < 5000) return;
    listedAt = Date.now();
    try { modes = (await api('/api/live-team')).chats || {}; } catch (_) { return; }
    render();
  }

  async function choose(mode) {
    const {chatId} = current;
    if (!chatId || changing || modeOf(chatId) === mode) return;
    changing = true; render();
    try {
      const result = await api('/api/live-team', {method: 'POST', body: JSON.stringify({action: 'chat_mode', chat: chatId, mode})});
      const chat = result.chat || {};
      modes[chatId] = {mode: chat.mode, team_id: chat.team_id, state: chat.team?.state || ''};
      if (chat.team) views.get(chatId)?.remember(chat.team);
    } catch (error) {
      host.alert?.(error.message || String(error));
    } finally { changing = false; render(); }
  }

  function viewFor(agentId, conversation) {
    const chatId = conversation.id;
    let held = views.get(chatId);
    if (held) { held.agentId = agentId; held.conversation = conversation; return held; }
    const root = el('div', undefined, 'chat-live-team-body');
    const head = el('div', undefined, 'chat-live-team-head');
    const about = el('p', '', 'hint chat-live-team-about');
    const fresh = el('button', 'Start a new team', 'compact');
    fresh.type = 'button';
    fresh.title = 'Your next message starts a fresh team. The current one stays saved in the Live team tab.';
    head.append(about, fresh);
    const facts = () => chatFacts(held.agentId, held.conversation);
    let saved = null;
    const view = host.nexusLiveTeam.createTeamView(api, {
      idPrefix: `ltChat-${chatId.replace(/[^A-Za-z0-9_-]/g, '')}-`,
      emptyText: 'No live team yet. Describe the goal below: every agent in this chat starts a live session, and they work on it together while you watch.',
      placeholder: 'Describe the goal, or message the team…',
      visible: () => !$('theBigChat')?.hidden && current.chatId === chatId && modeOf(chatId) === 'live_team',
      // No live events for this team (Nexus restarted, or it was closed): show it
      // as saved. Reopening is safe either way; a running team just gets the message.
      saved: team => (saved?.team_id === team ? saved : view.state.team?.team_id === team
        ? {...view.state.team, state: 'saved', agents: (view.state.team.agents || []).map(one => ({...one, session: {}}))} : null),
      onTeam: team => { if (modes[chatId]) modes[chatId].state = team.state; decorateSidebar(); },
      async start(words, to, state, attachments = []) {
        if (state.teamId) {
          const result = await view.act({action: 'reopen', team: state.teamId, text: words, to, ...(attachments.length ? {attachments} : {})});
          return result.team?.team_id || state.teamId;
        }
        const chat = facts();
        if (!chat.ids.length) throw new Error('This chat has no agents.');
        const result = await view.act({action: 'create', chat: chatId, agents: chat.ids, lead: chat.lead, goal: words,
          project: chat.project?.path || '', access: chat.access, mode: 'shared', ...(attachments.length ? {attachments} : {}),
          name: `${held.conversation.name || 'Chat'} · ${(words || 'Attached files').split('\n')[0].slice(0, 50)}`});
        const teamId = result.team?.team_id || '';
        modes[chatId] = {mode: 'live_team', team_id: teamId, state: result.team?.state || 'starting'};
        decorateSidebar();
        return teamId;
      },
    });
    fresh.addEventListener('click', async () => {
      if (!host.confirm('Start a new team for this chat? The current team keeps running until you close it and stays saved in the Live team tab.')) return;
      try {
        await api('/api/live-team', {method: 'POST', body: JSON.stringify({action: 'chat_new_team', chat: chatId})});
        if (modes[chatId]) { modes[chatId].team_id = ''; modes[chatId].state = ''; }
        saved = null; view.open(''); render();
        view.text.focus();
      } catch (error) { view.say(error.message, true); }
    });
    const panes = el('div', undefined, 'chat-live-team-panes');
    panes.append(view.main, view.board);
    root.append(head, panes);
    held = {agentId, conversation, root, view, about, fresh, loaded: false,
      remember(team) { saved = team?.state === 'saved' ? team : saved; }};
    views.set(chatId, held);
    return held;
  }

  async function loadChat(held) {
    const chatId = held.conversation.id;
    try {
      const chat = await api(`/api/live-team/chat?chat=${encodeURIComponent(chatId)}`);
      modes[chatId] = {mode: chat.mode, team_id: chat.team_id, state: chat.team?.state || ''};
      held.remember(chat.team);
      held.loaded = true;
      held.view.open(chat.team_id || '');
    } catch (error) { held.view.say(error.message, true); }
    render();
  }

  function describe(held) {
    const chat = chatFacts(held.agentId, held.conversation);
    const names = chat.ids.map(id => chat.names.get(id) || id);
    const access = {full: 'full project access', ask: 'asks before commands', read_only: 'read only'}[chat.access] || chat.access;
    held.about.textContent = `${names.join(' ↔ ') || 'No agents'} · ${chat.project ? `works in ${chat.project.path || chat.project.name}` : 'no project selected: agents start in the Nexus project folder'} · ${access}`;
    held.fresh.hidden = !held.view.state.teamId;
  }

  function decorateSidebar() {
    const list = $('theBigChatConversationList');
    if (!list) return;
    for (const pick of list.querySelectorAll('.the-big-chat-conversation-pick[data-chat-id]')) {
      const info = modes[pick.dataset.chatId];
      let tag = pick.querySelector('.chat-orchestrator-tag');
      if (info?.mode !== 'live_team') { tag?.remove(); continue; }
      if (!tag) { tag = el('span', '', 'chat-orchestrator-tag'); pick.append(tag); }
      const live = ['running', 'starting'].includes(info.state);
      tag.textContent = live ? 'Live team · running' : 'Live team';
      tag.classList.toggle('running', live);
    }
  }

  function render() {
    const bar = ensureControls();
    const main = host.document.querySelector('#theBigChat .the-big-chat-main');
    const box = $('theBigChatLiveTeam');
    if (!bar || !main || !box) return;
    const {chatId} = current;
    const mode = chatId ? modeOf(chatId) : 'nexus';
    bar.hidden = !chatId;
    for (const choice of bar.querySelectorAll('.chat-orchestrator-choice')) {
      choice.setAttribute('aria-pressed', String(choice.dataset.mode === mode));
      choice.disabled = changing || !chatId;
    }
    const live = mode === 'live_team';
    main.classList.toggle('chat-orchestrator-live', live);
    box.hidden = !live;
    const held = chatId ? views.get(chatId) : null;
    if (live && held) {
      if (held.root.parentElement !== box) { box.replaceChildren(held.root); held.view.poll(); }
      describe(held);
    } else if (!live) box.replaceChildren();
    decorateSidebar();
  }

  // Called by the expanded chat on every render; cheap when nothing changed.
  function sync(agentId, conversation) {
    if (!host.nexusLiveTeam?.createTeamView) return;
    const chatId = conversation?.id && !conversation.archived_at ? String(conversation.id) : '';
    const changed = chatId !== current.chatId;
    current = {agentId: String(agentId || ''), chatId};
    if (chatId) {
      const held = viewFor(current.agentId, conversation);
      if (!held.loaded && changed) void loadChat(held);
    }
    if (changed) void loadModes(false);
    render();
  }

  host.nexusChatOrchestrator = {sync, modes: () => ({...modes}), views};
})(typeof window !== 'undefined' ? window : globalThis);
