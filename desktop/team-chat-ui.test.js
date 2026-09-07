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
const helpers = section("const chatGoalRequests =", "function chatRecipientWords");
const handlers = {
  compact: section("async function sendWhatIsTypedTo", "async function startTheChatAgainFor"),
  maximized: section("async function sendFromTheBigChat", "function wireUpTheTray"),
};

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
  for (const status of ["queued", "running", "complete"]) {
    assert.equal(check({...routine, correlation: {...routine.correlation, goal_status: status}}), true, status);
  }
  for (const status of ["paused", "failed", "waiting_for_user", "waiting_for_project", "cancelled", "cancelling", "unknown"]) {
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
    assert.deepEqual(post.body.answers, {"decision-portable": "Use keyboard controls too"});
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
