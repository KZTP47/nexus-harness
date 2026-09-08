"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../src/our_harness/ui/app.js"), "utf8");
function section(start, end) {
  return source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
}
const helpers = source.match(/^const TEAM_FOLLOW_UP_CHARACTERS = .*;$/m)[0] + "\n"
  + section("const chatGoalRequests =", "function chatRecipientWords");
const handlers = {
  compact: section("async function sendWhatIsTypedTo", "async function startTheChatAgainFor"),
  maximized: section("async function sendFromTheBigChat", "function wireUpTheTray"),
};

test("new relay chats continue by default while explicit finite choices remain local to their chat", () => {
  const context = vm.createContext({swarmChatRoundPolicies: new Map(), DEFAULT_FINITE_TEAM_ROUNDS: 3,
    swarmChatKey(id) { return `chat:${id}`; }, syncChatRoundPolicy() {}});
  vm.runInContext(section("function chatRoundPolicyFor", "function syncChatRoundPolicy"), context);
  assert.equal(vm.runInContext("selectedChatRoundLimit('one')", context), null);
  vm.runInContext("updateChatRoundPolicy('one', false, 17)", context);
  assert.equal(vm.runInContext("selectedChatRoundLimit('one')", context), 17);
  assert.equal(vm.runInContext("selectedChatRoundLimit('two')", context), null);
  vm.runInContext("updateChatRoundPolicy('two', false, 4)", context);
  assert.equal(vm.runInContext("selectedChatRoundLimit('one')", context), 17);
});

test("goal status distinguishes versioned unlimited calls from finite and legacy exhausted budgets", () => {
  const context = vm.createContext({goal: {status: "running", progress: {complete: 1, total: 3},
    budget: {provider_calls: 1200, max_provider_calls: 0,
      call_limit_policy: {schema_version: 1, limits: {max_provider_calls: 0}}}}});
  vm.runInContext(section("function missionStatusWords", "function missionProviderSetupChanged"), context);
  assert.match(vm.runInContext("missionStatusWords(goal)", context), /1200 provider calls · no total call limit/);
  vm.runInContext("delete goal.budget.call_limit_policy", context);
  assert.match(vm.runInContext("missionStatusWords(goal)", context), /1200\/0 provider calls/);
  vm.runInContext("goal.budget.max_provider_calls = 3000", context);
  assert.match(vm.runInContext("missionStatusWords(goal)", context), /1200\/3000 provider calls/);
});

test("only correlated routine Nexus goal transitions use compact status rows", () => {
  const context = vm.createContext({});
  vm.runInContext(section("function isNexusChatTurn", "function aChatTurnFace")
    + section("function normalizedLongHorizonCorrelation", "const PARTICIPANT_OUTCOME_STATUSES"), context);
  const routine = {
    who: "them", speaker_id: "nexus", speaker_name: "Nexus", recipient_name: "You",
    phase: "long_horizon_status", text: "Full original status text.",
    correlation: {schema_version: 1, kind: "long_horizon_status", goal_id: "portable-goal", goal_status: "running"},
  };
  const check = (one) => {
    context.one = one;
    return vm.runInContext("isRoutineGoalStatusTurn(one)", context);
  };
  for (const status of ["queued", "running"]) {
    assert.equal(check({...routine, correlation: {...routine.correlation, goal_status: status}}), true, status);
  }
  for (const status of ["complete", "paused", "failed", "waiting_for_user", "waiting_for_project", "cancelled", "cancelling", "unknown"]) {
    assert.equal(check({...routine, correlation: {...routine.correlation, goal_status: status}}), false, status);
  }
  for (const changes of [
    {speaker_id: "agent-a", speaker_name: "Builder", recipient_name: "Reviewer"},
    {phase: "agent_discussion"}, {phase: "nexus_error"}, {correlation: null},
    {correlation: {...routine.correlation, schema_version: 2}},
    {correlation: {...routine.correlation, kind: "ordinary_chat"}},
    {correlation: {...routine.correlation, goal_id: ""}},
    {structured_state_unavailable: true}, {participant_outcome: {schema_version: 1}},
    {questions: [{id: "choice", text: "Choose an option"}]}, {attachments: [{name: "evidence.txt"}]},
  ]) assert.equal(check({...routine, ...changes}), false, JSON.stringify(changes));
});

test("an admission receipt points to the shared chat without claiming that the team is still running", () => {
  const context = vm.createContext({});
  vm.runInContext(section("function longHorizonAdmissionWords", "function finishLongHorizonAdmissionActivity"), context);
  for (const status of ["running", "queued", "waiting_for_user"]) {
    context.status = status;
    const words = vm.runInContext('longHorizonAdmissionWords({goal_id: "portable-goal", status})', context);
    assert.match(words.detail, /was accepted/);
    assert.match(words.detail, /this chat/);
    assert.doesNotMatch(words.detail, /is running|Mission control/);
  }
});

test("saved team activity survives admission collapse and reports actual waiting, retry and response states", () => {
  const goal = {status: "running", tasks: [], agents: [{id: "arbitrary-a", name: "Builder"}]};
  const context = vm.createContext({goal, theSwarmAgent() { return null; }, longHorizonStateWords(value) { return value; }});
  vm.runInContext(section("function chatGoalActivity", "function renderSwarmChatActivity"), context);
  const read = (problem = "") => {
    context.problem = problem;
    return vm.runInContext("chatGoalActivity({goal, problem})", context);
  };
  assert.equal(read().stage, "Team is working");
  assert.equal(read().elapsedLabel, "Team status");
  goal.tasks = [{assigned_agent_id: "arbitrary-a", state: "running", provider_effect_state: "dispatched"}];
  assert.equal(read().stage, "Builder is responding");
  goal.tasks[0].protocol_recovery = {schema_version: 1, state: "dispatched", attempts: 1, max_attempts: 2};
  assert.match(read().stage, /Correcting Builder/);
  assert.match(read().detail, /1 of 2/);
  goal.tasks[0].protocol_recovery.schema_version = 2;
  goal.tasks[0].protocol_recovery.cumulative_attempts = 3;
  assert.match(read().stage, /Correcting Builder/);
  assert.match(read().detail, /1 of 2/);
  goal.tasks[0].protocol_recovery.state = "corrected";
  assert.equal(read().stage, "Builder is responding");
  goal.tasks[0].provider_effect_state = "reply_received";
  assert.equal(read().stage, "Processing the team’s reply");
  goal.status = "paused";
  goal.note = "The saved response requires review.";
  assert.equal(read().state, "attention");
  assert.equal(read().detail, goal.note);
  assert.doesNotMatch(read().stage, /responding|retrying|working/);
  goal.pending_interrupts = [{id: "decision"}];
  assert.equal(read().stage, "Waiting for your answer");
  assert.equal(read("The chat belongs to a changed team.").stage, "Team needs attention");
  delete goal.pending_interrupts;
  goal.status = "waiting_for_project";
  assert.equal(read().state, "waiting");
  goal.status = "queued";
  goal.tasks = [];
  assert.equal(read().stage, "Team queued");
  goal.automatic_start_failure = {retry_automatically: true, error: "Worker temporarily unavailable"};
  goal.project_queue = {auto_start_pending: true};
  assert.equal(read().stage, "Waiting to retry team startup");
  assert.equal(vm.runInContext("chatGoalActivity({goal: null, problem: ''})", context), null);
});

test("a late activity update uses the goal still bound to that chat, not the initiating agent's new chat", () => {
  const rendered = [];
  const context = vm.createContext({
    swarmChats: [{agent: "switched-agent"}, {agent: "remaining-peer"}], swarmChatActivity: new Map(), theBigOne: "remaining-peer",
    swarmChatRuntimeKey(id) { return id === "switched-agent" ? "chat:new" : "chat:original"; },
    chatLongGoalContext(id) { return {stage: id === "remaining-peer" ? "Original goal working" : "Different goal paused"}; },
    chatGoalActivity(value) { return value; }, visibleSwarmChatActivity() { return null; },
    theChatCardFor(id) { return {querySelector() { return id; }}; }, $(id) { return id; },
    showActivityInPanel(panel, activity) { rendered.push({panel, stage: activity.stage}); },
  });
  vm.runInContext(section("function renderSwarmChatActivity", "async function pollSwarmChatActivity"), context);
  vm.runInContext("renderSwarmChatActivity('switched-agent', 'chat:original')", context);
  assert.deepEqual(rendered, [{panel: "remaining-peer", stage: "Original goal working"},
    {panel: "theBigChatActivity", stage: "Original goal working"}]);
});

test("goal repair retains its authenticated goal diagnosis after a successful connection test", async () => {
  const state = {calls: [], notices: [], rendered: null};
  const goal = {goal_id: "goal-portable", conversation_id: "chat-portable", project: {id: "project-portable"}, requested_agent_ids: ["agent-a", "agent-b"]};
  const plan = {repair: {state: "goal-action-invalid", goal_issue: {goal_id: goal.goal_id}, actions: []}};
  const nodes = new Map();
  const context = vm.createContext({state, goal, plan, AbortController,
    swarmAgentRepairTests: new Map(), swarmAgentRepairPlans: new Map(),
    $(id) { if (!nodes.has(id)) nodes.set(id, {dataset: {}, textContent: ""}); return nodes.get(id); },
    theSwarmAgent() { return {id: "agent-a", name: "Builder"}; },
    agentStillUsesRoute() { return true; }, chatLongGoalContext() { return {goal}; },
    renderAgentRepairPanel(agent, route, value) { state.rendered = value; },
    sayInSwarm(words) { state.notices.push(words); }, refreshSwarm: async () => {},
    async request(url, options) { state.calls.push({url, body: options ? JSON.parse(options.body) : null}); return {plan, goal}; },
    rememberChatGoalSnapshot() {}, refreshLongGoals: async () => {}, openChatGoalDetails: async () => {},
  });
  vm.runInContext(section("function agentRepairContext", "function renderAgentRepairPanel")
    + section("async function loadAgentRepairPlan", "async function checkAgentLogin")
    + section("async function performAgentRepairAction", "async function stopAgentRouteTest"), context);
  await vm.runInContext("loadAgentRepairPlan('agent-a','arbitrary-route')", context);
  assert.deepEqual(state.calls[0].body, {route: "arbitrary-route", agent_id: "agent-a", goal_id: "goal-portable"});
  await vm.runInContext("runAgentRouteTest('agent-a','arbitrary-route')", context);
  assert.deepEqual(state.calls[1].body, state.calls[0].body);
  assert.equal(state.rendered.repair.state, "goal-action-invalid");
  assert.match(state.notices.at(-1), /connection verified.*saved goal still needs/);
  context.offered = {id: "resume-goal", goal_id: goal.goal_id, conversation_id: goal.conversation_id, diagnosis_fingerprint: "diagnosis-bound-to-goal"};
  context.button = {textContent: "Resume saved goal", isConnected: true};
  await vm.runInContext("performAgentRepairAction('agent-a','arbitrary-route',offered,button)", context);
  const resumed = state.calls.find(one => one.url === "/api/long-horizon/control").body;
  assert.deepEqual(resumed, {goal_id: goal.goal_id, action: "resume", payload: {
    chat_id: goal.conversation_id, project_id: goal.project.id, participant_ids: goal.requested_agent_ids,
    repair_context: {route: "arbitrary-route", agent_id: "agent-a", goal_id: goal.goal_id, diagnosis_fingerprint: "diagnosis-bound-to-goal"},
  }});
  state.calls.length = 0;
  context.offered.conversation_id = "stale-other-chat";
  await vm.runInContext("performAgentRepairAction('agent-a','arbitrary-route',offered,button)", context);
  assert.equal(state.calls.some(one => one.url === "/api/long-horizon/control"), false);
  assert.match(nodes.get("swarmAgentSessionStatus").textContent, /chat changed/);
});

function fixture(view = "maximized") {
  const state = {
    agent: {id: "builder-portable", name: "Builder", who: "vendor-one", ready: true},
    conversation: {id: "chat-arbitrary", project: "project-arbitrary", pair: ["builder-portable", "reviewer-portable"]},
    box: {value: "Use keyboard controls too", focus() {}, setSelectionRange() {}},
    calls: [], notices: [], inventoryHook: null, controlHook: null, prepareHook: null,
  };
  state.goal = {
    goal_id: "goal-arbitrary", conversation_id: state.conversation.id,
    project: {id: state.conversation.project}, lead_agent_id: state.agent.id,
    requested_agent_ids: [...state.conversation.pair], status: "running", revision: 12,
    pending_interrupts: [],
  };
  const dom = new Map();
  const compactBox = view === "compact" ? state.box : {value: ""};
  dom.set("theBigChatBox", view === "maximized" ? state.box : {value: ""});
  const card = {querySelector() { return view === "compact" ? state.box : compactBox; }};
  const context = {
    console, setTimeout, clearTimeout, localStorage: {getItem() { return null; }},
    state, window: {}, theBigOne: state.agent.id,
    longGoals: [state.goal], longGoal: {goal_id: "unrelated-selected-goal"},
    swarmBusy: new Set(), swarmStopping: new Set(), swarmChatResetting: new Set(),
    swarmConversationSwitching: new Set(), swarmChatAttachments: new Map(),
    swarmChatComposerDrafts: new Map(), theBigChatComposerDrafts: new Map(),
    $(id) { if (!dom.has(id)) dom.set(id, {textContent: "", disabled: false}); return dom.get(id); },
    theSwarmAgent() { return state.agent; }, theChatCardFor() { return card; },
    activeConversationFor() { return state.conversation; },
    isLoneAgentChat() { return state.conversation.pair.length === 1; },
    swarmChatKey() { return `${state.agent.id}:${state.conversation.id}`; },
    swarmChatRuntimeKey() { return context.swarmChatKey(); },
    swarmChatIsHydrating() { return false; },
    swarmChatAttachmentsAreLoading() { return Boolean(state.attachmentLoading); },
    projectWorkPauseForMessage() { return ""; },
    limitsForSwarmChat() { return {input_characters: 200000}; },
    syncChatTeamReadiness() { return []; },
    confirmProjectWork() { return {allowed: true, confirmed: false}; },
    nextSwarmChatRevision() {}, setWhatCanBePressedInSwarm() {},
    rememberSwarmChatComposer() {
      if (state.rememberLive) context.swarmChatComposerDrafts.set(context.swarmChatKey(), {value: state.box.value});
    },
    rememberTheBigChatComposer() {
      if (state.rememberLive) context.theBigChatComposerDrafts.set(context.swarmChatKey(), {value: state.box.value});
    },
    beginSwarmChatActivity() { return {id: "ordinary-activity"}; },
    sayInRuntimeChat(key, text) { state.notices.push(text); },
    sayInTheChatFor(key, text) { state.notices.push(text); },
    refreshTheChatFor: async () => {}, refreshLongGoals: async () => {},
    swarmActivityCanReconcileSuccess() { return true; },
    swarmActivityCanSettle() { return true; },
    finishSwarmChatActivity() {}, rememberWorkRecoveryForKey() {},
    clearSwarmActivityAttachments() {}, keepWhatWasSaidToRuntime() {},
    renderWorkRecovery() {}, workResponseWords() { return "Answered"; },
    refreshSwarm() {}, normalizedParticipantOutcome() { return null; },
    automaticRoundStopWords() { return ""; }, selectedChatRoundLimit() { return 3; },
    finishSwarmActivityResponse() {}, swarmActivityIsCurrent() { return true; },
    directLongGoalIntent: async () => "exact-portable-intent",
    directLongGoalRequestId() { return "exact-portable-request"; },
    prepareDirectLongGoalAdmission: async (payload) => {
      state.calls.push({url: "prepare", payload, draft: state.box.value});
      if (state.prepareHook) await state.prepareHook();
      return {pending: {payload_sha256: "exact-portable-payload"}};
    },
    startAndReconcileDirectLongGoalAdmission: async () => {
      state.calls.push({url: "start", draft: state.box.value});
      return {goal: state.goal};
    },
    clearDirectLongGoalRequestMarker() { return true; }, forgetDirectLongGoalRecovery() {},
    bestEffortAcknowledgeDirectLongGoalTerminal: async () => {},
    selectLongGoalSnapshot(goal) { context.longGoal = goal; },
    finishLongHorizonAdmissionActivity() {}, longHorizonAdmissionWords() { return {detail: "accepted"}; },
    refreshDirectLongGoalRecoveries: async () => {},
    restoreSwarmChatDraft() {},
    stoppedChatError() { return false; }, showError(value) { state.notices.push(String(value)); },
    request: async (url, options = {}) => {
      const body = options.body ? JSON.parse(options.body) : null;
      state.calls.push({url, body, draft: state.box.value});
      if (url === "/api/long-horizon/goals") {
        if (state.inventoryHook) await state.inventoryHook();
        return {goals: state.inventory === undefined ? [state.goal] : state.inventory};
      }
      if (["/api/long-horizon/control", "/api/long-horizon/answer"].includes(url)) {
        if (state.controlHook) await state.controlHook(body);
        return {goal: {...state.goal, status: body.action === "pause" ? "paused" : "running"}};
      }
      if (url === "/api/swarm/say") return {said: []};
      throw new Error(`Unexpected request: ${url}`);
    },
  };
  vm.createContext(context);
  vm.runInContext(helpers + "\n" + handlers[view], context);
  state.key = context.swarmChatKey();
  context.swarmChatComposerDrafts.set(state.key, {value: state.box.value});
  context.theBigChatComposerDrafts.set(state.key, {value: state.box.value});
  state.context = context;
  state.replaceBox = (value = state.box.value) => {
    const detached = state.box;
    state.box = {...state.box, value};
    if (view === "maximized") dom.set("theBigChatBox", state.box);
    return detached;
  };
  state.send = (mode = "chat") => vm.runInContext(view === "compact"
    ? `sendWhatIsTypedTo(state.agent.id, ${JSON.stringify(mode)})`
    : `sendFromTheBigChat(${JSON.stringify(mode)})`, context);
  return state;
}

for (const view of ["compact", "maximized"]) {
  test(`${view}: pending file reads keep the draft and prevent premature send`, async () => {
    const f = fixture(view);
    f.attachmentLoading = true;
    await f.send();
    assert.equal(f.calls.length, 0);
    assert.equal(f.box.value, "Use keyboard controls too");
    const notice = view === "compact" ? f.notices.join(" ") : f.context.$("theBigChatSaidBack").textContent;
    assert.match(notice, /attached files.*loading/);
    f.attachmentLoading = false;
    f.inventory = [];
    f.context.longGoals = [];
    const attachment = {name: "copied.txt", type: "text/plain", size: 4, data: "data:text/plain;base64,dGVzdA=="};
    f.context.swarmChatAttachments.set(f.key, [attachment]);
    await f.send();
    const request = f.calls.find(one => one.url === "/api/swarm/say");
    assert.equal(request.body.chat, f.conversation.id);
    assert.deepEqual(request.body.attachments, [attachment]);
  });
  for (const outcome of ["no goal", "discovered goal", "switched chat"]) {
    test(`${view}: a paste during delayed goal discovery keeps its draft and files (${outcome})`, async () => {
      const f = fixture(view);
      f.inventory = outcome === "discovered goal" ? [f.goal] : [];
      f.context.longGoals = [];
      f.context.swarmChatAttachmentLoads = new Map();
      f.context.renderChatAttachments = () => {};
      let finishInventory, finishRead;
      f.inventoryHook = () => new Promise(resolve => { finishInventory = resolve; });
      f.context.FileReader = class {
        constructor() { this.listeners = new Map(); }
        addEventListener(name, callback) { this.listeners.set(name, callback); }
        readAsDataURL() {
          finishRead = () => {
            this.result = "data:text/plain;base64,dGVzdA==";
            this.listeners.get("load")();
          };
        }
      };
      vm.runInContext(section("function swarmChatAttachmentsAreLoading", "function swarmChatActivityFor")
        + section("function readChatAttachment", "function attachmentChip"), f.context);
      const sending = f.send();
      assert.equal(typeof finishInventory, "function", "the real send handler must reach the delayed inventory");
      let prevented = false;
      f.context.pasteChatAttachments(f.agent.id, {
        currentTarget: f.box,
        clipboardData: {files: [{name: "copied.txt", type: "text/plain", size: 4}]},
        preventDefault() { prevented = true; },
      });
      assert.equal(prevented, true);
      assert.equal(f.context.swarmChatAttachmentLoads.get(f.key), 1);
      if (outcome === "switched chat") f.conversation = {...f.conversation, id: "other-chat"};
      finishInventory();
      await sending;
      assert.deepEqual(f.calls.map(one => one.url), ["/api/long-horizon/goals"]);
      assert.equal(f.box.value, "Use keyboard controls too");
      for (const drafts of [f.context.swarmChatComposerDrafts, f.context.theBigChatComposerDrafts]) {
        assert.equal(drafts.get(f.key).value, f.box.value);
      }
      assert.equal(f.context.swarmBusy.size, 0);
      if (outcome !== "switched chat") assert.match(f.notices.join(" "), /attached files.*loading/);
      finishRead();
      await new Promise(setImmediate);
      assert.equal(f.context.swarmChatAttachmentLoads.size, 0);
      const attachment = f.context.swarmChatAttachments.get(f.key)[0];
      assert.equal(attachment.data, "data:text/plain;base64,dGVzdA==");
      assert.equal(f.calls.length, 1, "finishing the file read must not silently submit the draft");
      if (outcome === "no goal") {
        f.inventoryHook = null;
        await f.send();
        const sent = f.calls.find(one => one.url === "/api/swarm/say");
        assert.equal(sent.body.chat, f.conversation.id);
        assert.deepEqual(sent.body.attachments, [JSON.parse(JSON.stringify(attachment))]);
      } else if (outcome === "switched chat") {
        assert.equal(f.context.swarmChatAttachments.has(f.context.swarmChatKey()), false);
      }
    });
  }
  for (const newerOwner of [false, true]) {
    test(`${view}: collapsed admission releases only its own busy lease after delayed refresh (newer owner: ${newerOwner})`, async () => {
      const f = fixture(view);
      f.goal.status = "paused";
      f.inventory = [];
      f.context.longGoals = [];
      f.context.swarmChatActivity = new Map();
      f.context.window = {clearInterval() {}, clearTimeout() {},
        setTimeout(callback) { f.collapse = callback; return 1; }};
      f.context.renderSwarmChatActivity = () => {};
      f.context.renderTheBigChat = () => {};
      f.context.renderTurnsThatArrived = () => {};
      f.context.selectedChatIs = () => true;
      f.context.beginSwarmChatActivity = () => {
        f.activity = {id: "original-admission", chatKey: f.key, responseFinished: false};
        f.context.swarmChatActivity.set(f.key, f.activity);
        return f.activity;
      };
      vm.runInContext(section("function swarmActivityIsCurrent", "function selectedChatIs")
        + section("function scheduleSwarmChatActivityCollapse", "function settleSwarmChatActivityFromFeed")
        + section("function finishLongHorizonAdmissionActivity", "function markSwarmChatActivityStopping"), f.context);
      let entered;
      let release;
      const refreshing = new Promise(resolve => { entered = resolve; });
      const delayed = new Promise(resolve => { release = resolve; });
      f.context.refreshLongGoals = async () => {
        f.context.longGoals = [f.goal];
        entered();
        await delayed;
      };
      const sending = f.send("work");
      await refreshing;
      assert.equal(f.context.swarmBusy.has(f.key), true);
      assert.equal(f.activity.responseFinished, false);
      assert.equal(f.activity.terminalState, "admitted");
      f.context.swarmStopping.add(f.key);
      f.collapse();
      assert.equal(f.activity.collapsed, true);
      assert.equal(f.context.swarmChatActivity.get(f.key), f.activity,
        "the collapsed activity must retain reconciliation identity until the response finishes");
      f.box.value = "A second goal must wait for the current admission";
      await f.send("work");
      assert.equal(f.calls.filter(call => call.url === "prepare").length, 1);
      assert.equal(f.calls.filter(call => call.url === "start").length, 1,
        "a pending admission must not permit duplicate dispatch");
      const replacement = {id: "newer-independent-activity", chatKey: f.key};
      if (newerOwner) {
        f.context.swarmChatActivity.set(f.key, replacement);
        f.context.swarmStopping.add(f.key);
      }
      release();
      await sending;
      assert.equal(f.activity.responseFinished, true);
      assert.equal(f.context.swarmBusy.has(f.key), newerOwner);
      assert.equal(f.context.swarmStopping.has(f.key), newerOwner);
      assert.equal(f.context.swarmChatActivity.get(f.key), newerOwner ? replacement : undefined);
      if (newerOwner) return;
      const send = {disabled: true, setAttribute() {}};
      const stop = {textContent: "Stop", disabled: true};
      const card = {querySelector(selector) {
        return selector === ".swarm-chat-send" ? send : selector === ".swarm-chat-stop" ? stop : null;
      }};
      f.context.fillChatGoalPanel = () => {};
      f.context.swarmChatIsBusy = () => f.context.swarmBusy.has(f.key);
      f.context.swarmChatIsResetting = () => false;
      f.context.testCard = card;
      vm.runInContext("syncChatGoalControls(state.agent.id, testCard)", f.context);
      assert.equal(stop.textContent, "Resume team");
      assert.equal(stop.disabled, false);
      assert.equal(send.disabled, false);
      f.context.refreshLongGoals = async () => {};
      f.inventory = [f.goal];
      await vm.runInContext("controlChatGoal(state.agent.id)", f.context);
      const resume = f.calls.filter(call => call.body?.action === "resume");
      assert.equal(resume.length, 1);
      assert.equal(resume[0].body.goal_id, f.goal.goal_id);
      f.box.value = "Keep the amber theme after resuming";
      await f.send();
      assert.equal(f.calls.filter(call => call.body?.action === "steer").length, 1);
      assert.equal(f.calls.filter(call => call.url === "start").length, 1);
    });
  }
  for (const mode of ["work", "chat"]) {
    for (const mutation of ["same draft", "newer draft", "switched chat"]) {
      test(`${view}: accepted ${mode} clears only the exact live composer after ${mutation} replacement`, async () => {
        const f = fixture(view);
        const typed = "  Build the amber arena with keyboard controls.\n";
        f.box.value = typed;
        f.rememberLive = true;
        for (const drafts of [f.context.swarmChatComposerDrafts, f.context.theBigChatComposerDrafts]) {
          drafts.set(f.key, {value: typed});
        }
        if (mode === "work") {
          f.inventory = [];
          f.context.longGoals = [];
        }
        let reached;
        let release;
        const entered = new Promise((resolve) => { reached = resolve; });
        const waiting = new Promise((resolve) => { release = resolve; });
        const hook = async () => { reached(); await waiting; };
        if (mode === "work") f.prepareHook = hook;
        else f.controlHook = hook;
        const sending = f.send(mode);
        await entered;
        const replacement = mutation === "newer draft" ? "My next independent message" : typed;
        const detached = f.replaceBox(replacement);
        if (mutation === "switched chat") f.conversation = {...f.conversation, id: "another-chat"};
        const currentKey = f.context.swarmChatKey();
        const drafts = view === "compact" ? f.context.swarmChatComposerDrafts : f.context.theBigChatComposerDrafts;
        drafts.set(currentKey, {value: replacement});
        release();
        await sending;
        assert.equal(detached.value, typed, "detached textarea must not be used as the live composer");
        if (mutation === "same draft") {
          assert.equal(f.box.value, "", "the accepted prompt must not remain in the replacement textarea");
          assert.equal(drafts.get(currentKey)?.value || "", "", "remember must not resurrect the accepted prompt");
        } else {
          assert.equal(f.box.value, replacement, "an independent live draft must survive acceptance");
          assert.equal(drafts.get(currentKey)?.value, replacement);
        }
        const admission = f.calls.find((call) => call.url === "prepare");
        if (mode === "work") {
          assert.equal(admission.payload.text, typed.trim());
          assert.equal(f.calls.filter((call) => call.url === "start").length, 1);
        } else {
          const controls = f.calls.filter((call) => call.url === "/api/long-horizon/control");
          assert.equal(controls.length, 1);
          assert.equal(controls[0].body.payload.text, typed.trim());
        }
        assert.equal(f.calls.filter((call) => call.url === "/api/swarm/say").length, 0);
      });
    }
  }

  test(`${view}: real composer steers exact active team and clears only accepted draft`, async () => {
    const f = fixture(view);
    // The complete inventory still contains the unrelated detail selection.
    f.inventory = [f.goal, f.context.longGoal];
    f.context.longGoals = [...f.inventory];
    await f.send();
    const posts = f.calls.filter((one) => one.body);
    assert.equal(posts.length, 1);
    assert.equal(posts[0].url, "/api/long-horizon/control");
    assert.equal(posts[0].body.goal_id, f.goal.goal_id);
    assert.equal(posts[0].body.action, "steer");
    assert.deepEqual(posts[0].body.payload, {
      chat_id: f.conversation.id, project_id: f.conversation.project,
      participant_ids: [...f.conversation.pair].sort(), text: "Use keyboard controls too",
    });
    assert.equal(posts[0].draft, "Use keyboard controls too");
    assert.equal(f.box.value, "");
    assert.equal(f.context.swarmChatComposerDrafts.has(f.key), false);
    assert.equal(f.context.theBigChatComposerDrafts.has(f.key), false);
    assert.equal(f.context.longGoal.goal_id, "unrelated-selected-goal");
  });

  test(`${view}: inactive pair keeps ordinary one-agent chat behavior`, async () => {
    const f = fixture(view);
    f.inventory = [];
    f.context.longGoals = [];
    await f.send();
    const post = f.calls.find((one) => one.body);
    assert.equal(post.url, "/api/swarm/say");
    assert.equal(post.body.mode, "chat");
    assert.equal(post.body.chat, f.conversation.id);
  });

  test(`${view}: uncertain response preserves exact draft and attached files never disappear`, async () => {
    const failed = fixture(view);
    failed.controlHook = async () => { throw new Error("Connection lost"); };
    await failed.send();
    assert.equal(failed.box.value, "Use keyboard controls too");
    assert.ok(failed.notices.some((text) => text.includes("Connection lost")));
    const files = fixture(view);
    const attached = [{name: "input.txt", data: "ZXhhY3Q="}];
    files.context.swarmChatAttachments.set(files.key, attached);
    await files.send();
    assert.equal(files.calls.filter((one) => one.body).length, 0);
    assert.equal(files.box.value, "Use keyboard controls too");
    assert.deepEqual(files.context.swarmChatAttachments.get(files.key), attached);
    assert.ok(files.notices.some((text) => text.includes("files and draft have been kept")));
  });

  test(`${view}: changed project, pair, completed goal, and changed questions never become direct chat`, async () => {
    for (const change of [
      (f) => { f.goal.project = {id: "different-project"}; },
      (f) => { f.goal.requested_agent_ids = [f.agent.id, "different-peer"]; },
      (f) => { f.inventory = [{...f.goal, status: "complete"}]; },
      (f) => { f.goal.pending_interrupts = [{id: "decision-1", questions: [{id: "a"}, {id: "b"}]}]; },
    ]) {
      const f = fixture(view);
      change(f);
      await f.send();
      assert.equal(f.calls.filter((one) => one.body).length, 0);
      assert.equal(f.box.value, "Use keyboard controls too");
    }
  });

  test(`${view}: one actual question receives the answer with its revision and pending identity`, async () => {
    const f = fixture(view);
    f.goal.pending_interrupts = [{id: "decision-portable", questions: [{id: "controls", prompt: "Keyboard or mouse?"}]}];
    await f.send();
    const post = f.calls.find((one) => one.body);
    assert.equal(post.url, "/api/long-horizon/answer");
    assert.equal(post.body.expected_revision, 12);
    assert.deepEqual(post.body.pending_ids, ["decision-portable"]);
    assert.deepEqual(post.body.answers, {"decision-portable": {schema_version: 1, audience: "team",
      questions: [{question_id: "controls", selected_options: [], text: "Use keyboard controls too"}]}});
    assert.match(post.body.request_id, /^[a-f0-9-]{36}$/);
  });

  test(`${view}: a newer draft and chat switch survive a delayed accepted steering response`, async () => {
    const f = fixture(view);
    f.controlHook = async () => {
      f.box.value = "A newer independent draft";
      f.context.theBigChatComposerDrafts.set(f.key, {value: f.box.value});
      f.context.swarmChatComposerDrafts.set(f.key, {value: f.box.value});
      f.conversation = {...f.conversation, id: "a-different-chat"};
    };
    await f.send();
    assert.equal(f.box.value, "A newer independent draft");
    assert.equal(f.context.theBigChatComposerDrafts.get(f.key).value, f.box.value);
    assert.equal(f.context.swarmChatComposerDrafts.get(f.key).value, f.box.value);
  });
}

test("active team pause and resume target the chat goal, independent of advanced panel selection", async () => {
  const f = fixture();
  await vm.runInContext("controlChatGoal(state.agent.id)", f.context);
  assert.equal(f.notices.at(-1), "Pause was accepted. Follow the team's current status in this chat.");
  assert.equal(f.calls.at(-1).body.action, "pause");
  assert.equal(f.calls.at(-1).body.goal_id, f.goal.goal_id);
  await vm.runInContext("controlChatGoal(state.agent.id)", f.context);
  assert.equal(f.notices.at(-1), "Resume was accepted. Follow the team's current status in this chat.");
  assert.equal(f.calls.at(-1).body.action, "resume");
  assert.equal(f.calls.at(-1).body.payload.chat_id, f.conversation.id);
});

test("cold renderer discovers existing team goal before sending and changed chat during discovery sends nothing", async () => {
  const restored = fixture();
  restored.context.longGoals = [];
  await restored.send();
  assert.equal(restored.calls.find((one) => one.body).body.action, "steer");
  const switched = fixture();
  switched.inventoryHook = async () => { switched.conversation = {...switched.conversation, id: "other-chat"}; };
  await switched.send();
  assert.equal(switched.calls.filter((one) => one.body).length, 0);
  assert.equal(switched.box.value, "Use keyboard controls too");
});

for (const setup of ["provider", "collaboration"]) {
  test(`${setup} contract drift explains blocked continuation while preserving Pause team`, async () => {
    const f = fixture();
    const explanation = `The saved ${setup} contract changed; inspect the saved goal and start fresh.`;
    if (setup === "provider") {
      f.goal.provider_setup_changed = true;
      f.goal.provider_setup_status = {message: explanation};
    } else {
      f.goal.collaboration_contract_changed = true;
      f.goal.collaboration_contract_status = {message: explanation};
    }
    assert.equal(vm.runInContext("chatLongGoalContext(state.agent.id).problem", f.context), explanation);
    await f.send();
    assert.equal(f.calls.filter((one) => one.body).length, 0);
    assert.equal(f.box.value, "Use keyboard controls too");
    await vm.runInContext("controlChatGoal(state.agent.id)", f.context);
    assert.equal(f.calls.at(-1).body.action, "pause");
    const postsAfterPause = f.calls.filter((one) => one.body).length;
    await vm.runInContext("controlChatGoal(state.agent.id)", f.context);
    assert.equal(f.calls.filter((one) => one.body).length, postsAfterPause);
    assert.equal(f.notices.at(-1), explanation);
  });
}
