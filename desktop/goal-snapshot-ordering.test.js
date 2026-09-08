"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../src/our_harness/ui/app.js"), "utf8");
function section(start, end) {
  const from = source.indexOf(start), to = source.indexOf(end, from);
  assert.ok(from >= 0 && to > from, start);
  return source.slice(from, to);
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}
const tick = () => new Promise(setImmediate);

function fixture() {
  const requests = [], storage = new Map(), renders = [];
  const context = vm.createContext({
    console,
    localStorage: {getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value)},
    window: {clearTimeout() {}},
    $: () => ({textContent: ""}),
    activeConversationFor: () => null,
    refreshLongGoalOriginChats: async () => {},
    missionSelectedGoalId: () => storage.get("selected") || context.readSelected()?.goal_id || "",
    renderMissionControl: () => renders.push(context.readSelected()),
    watchLongGoal() {},
    request(url) { const pending = deferred(); requests.push({url, ...pending}); return pending.promise; },
  });
  vm.runInContext(
    'const LONG_GOAL_SELECTED_KEY = "selected";\n'
    + section("let longGoals =", "async function readSwarmBoardRun")
    + section("const chatGoalRequests =", "async function openChatGoalDetails")
    + section("function longGoalNeedsWatching", "function watchLongGoal")
    + '\nfunction readSelected() { return longGoal; }\n'
    + 'function readInventory() { return longGoals; }\n'
    + 'function seed(goals, selected = goals[0] || null) { longGoals = goals; longGoal = selected; }\n',
    context,
  );
  return {context, requests, renders};
}
function goal(id = "goal-a", revision = 1, extra = {}) {
  return {
    goal_id: id, revision, objective_epoch: 1,
    conversation_id: `chat-${id}`, project: {id: "selected-project"},
    requested_agent_ids: ["builder", "reviewer"], lead_agent_id: "builder",
    status: "waiting_for_user", interrupts: [{id: "question-a", state: "pending"}],
    pending_interrupts: [{id: "question-a", questions: [{id: "location", prompt: "Where is the project?"}]}],
    ...extra,
  };
}
function answered(prior, revision = prior.revision + 1) {
  return {...prior, revision, status: "running", interrupts: [{id: "question-a", state: "resolved"}], pending_interrupts: []};
}
function assertLatest(f, expected, sibling) {
  assert.equal(f.context.readSelected()?.revision, expected.revision);
  assert.equal(f.context.readSelected()?.status, expected.status);
  assert.equal(f.context.readSelected()?.interrupts?.[0]?.state, expected.interrupts?.[0]?.state);
  assert.equal(f.context.readSelected()?.pending_interrupts?.length, expected.pending_interrupts?.length);
  const cached = f.context.readInventory().find(one => one.goal_id === expected.goal_id);
  assert.equal(cached?.revision, expected.revision);
  assert.equal(cached?.interrupts?.[0]?.state, expected.interrupts?.[0]?.state);
  assert.equal(cached?.pending_interrupts?.length, expected.pending_interrupts?.length);
  if (sibling) assert.equal(f.context.readInventory().find(one => one.goal_id === sibling.goal_id), sibling);
}

test("an older snapshot cannot reopen a question after its answer acknowledgement", () => {
  const f = fixture(), old = goal(), latest = answered(old), sibling = goal("goal-b");
  f.context.seed([old, sibling]);
  f.context.rememberChatGoalSnapshot(latest);
  f.context.rememberChatGoalSnapshot(old);
  assertLatest(f, latest, sibling);
});

for (const binding of ["conversation", "project"]) {
  test(`a reused goal ID cannot overwrite a different ${binding} binding`, () => {
    const f = fixture(), current = goal();
    f.context.seed([current]);
    const foreign = {...answered(current, 100), ...(binding === "conversation"
      ? {conversation_id: "another-chat"} : {project: {id: "another-project"}})};
    f.context.rememberChatGoalSnapshot(foreign);
    assert.equal(f.context.readSelected(), current);
    assert.equal(f.context.readInventory()[0], current);
  });
}

for (const binding of ["source path", "project authority"]) {
  test(`a reused project ID cannot overwrite the goal's ${binding}`, () => {
    const f = fixture();
    const current = goal("goal-a", 1, {
      project: {id: "selected-project", path: path.join(require("node:os").tmpdir(), "nexus-snapshot-selected")},
      project_authority_id: "authority-original",
    });
    f.context.seed([current]);
    const foreign = {...answered(current, 100), ...(binding === "source path"
      ? {project: {...current.project, path: path.join(require("node:os").tmpdir(), "nexus-snapshot-other")}}
      : {project_authority_id: "authority-other"})};
    f.context.rememberChatGoalSnapshot(foreign);
    assert.equal(f.context.readSelected(), current);
    assert.equal(f.context.readInventory()[0], current);
  });
}

test("newer revisions can change status and objective epoch without touching another saved chat", () => {
  const f = fixture(), current = goal(), sibling = goal("goal-b", 50);
  f.context.seed([current, sibling]);
  const next = {...answered(current, 3), objective_epoch: 2, status: "paused"};
  f.context.rememberChatGoalSnapshot(next);
  assertLatest(f, next, sibling);
  assert.equal(f.context.readSelected().objective_epoch, 2);
});

test("a higher-revision new objective can become nonterminal after an earlier terminal snapshot", () => {
  const f = fixture(), current = goal("goal-a", 8, {status: "complete", interrupts: []});
  f.context.seed([current]);
  const newer = goal("goal-a", 9, {status: "running", objective_epoch: 2, interrupts: []});
  f.context.rememberChatGoalSnapshot(newer);
  assert.equal(f.context.readSelected(), newer);
  assert.equal(f.context.readInventory()[0], newer);
});

test("a fresh same-revision projection can update provider setup information", () => {
  const f = fixture(), current = goal();
  f.context.seed([current]);
  const projected = {...current, provider_setup_changed: true, provider_setup_status: {message: "Reconnect"}};
  f.context.rememberChatGoalSnapshot(projected);
  assert.equal(f.context.readSelected().provider_setup_changed, true);
});

test("an unversioned or malformed snapshot cannot replace known versioned state", () => {
  for (const revision of [undefined, null, "20", -1, Number.NaN]) {
    const f = fixture(), current = goal();
    f.context.seed([current]);
    f.context.rememberChatGoalSnapshot({...answered(current), revision});
    assert.equal(f.context.readSelected(), current);
    assert.equal(f.context.readInventory()[0], current);
  }
});

test("an inventory request begun before an answer cannot replace the acknowledged newer revision", async () => {
  const f = fixture(), old = goal(), sibling = goal("goal-b"), latest = answered(old);
  f.context.seed([old, sibling]);
  const refresh = f.context.refreshLongGoals(false);
  assert.equal(f.requests[0].url, "/api/long-horizon/goals");
  f.context.rememberChatGoalSnapshot(latest);
  f.requests[0].resolve({goals: [old, sibling]});
  await refresh;
  assertLatest(f, latest, sibling);
});

test("an old equal-revision inventory cannot undo a more recently observed policy projection", async () => {
  const f = fixture(), old = goal();
  f.context.seed([old]);
  const refresh = f.context.refreshLongGoals(false);
  f.context.rememberChatGoalSnapshot({...old, provider_setup_changed: true});
  f.requests[0].resolve({goals: [old]});
  await refresh;
  assert.equal(f.context.readSelected().provider_setup_changed, true);
});

test("a detail read held on its event page cannot overwrite an answer received while it waits", async () => {
  const f = fixture(), old = goal(), latest = answered(old), sibling = goal("goal-b");
  f.context.seed([old, sibling]);
  const loading = f.context.loadLongGoal(old.goal_id);
  f.requests[0].resolve({goal: old});
  await tick();
  assert.match(f.requests[1].url, /\/api\/long-horizon\/events\?/);
  f.context.rememberChatGoalSnapshot(latest);
  f.requests[1].resolve({events: [], next: 0, has_more: false});
  await loading;
  assertLatest(f, latest, sibling);
});

test("a higher server revision wins even when its detail request began before the last acknowledgement", async () => {
  const f = fixture(), old = goal(), acknowledged = answered(old, 2), newest = answered(old, 3);
  f.context.seed([old]);
  const loading = f.context.loadLongGoal(old.goal_id);
  f.context.rememberChatGoalSnapshot(acknowledged);
  f.requests[0].resolve({goal: newest});
  await tick();
  f.requests[1].resolve({events: [], next: 0, has_more: false});
  await loading;
  assertLatest(f, newest);
});

test("a detail response for another requested goal ID cannot change the selected goal", async () => {
  const f = fixture(), current = goal(), foreign = goal("goal-foreign", 100);
  f.context.seed([current]);
  const loading = f.context.loadLongGoal(current.goal_id).catch(() => {});
  f.requests[0].resolve({goal: foreign});
  await tick();
  // An implementation can surface a read error or decline the response. It
  // must not adopt the foreign identity, even if it reads another event page.
  if (f.requests[1]) f.requests[1].resolve({events: [], next: 0, has_more: false});
  await loading;
  assert.equal(f.context.readSelected(), current);
  assert.equal(f.context.readInventory().length, 1);
  assert.equal(f.context.readInventory()[0], current);
});

test("an inventory request cannot erase a saved goal admitted after that request began", async () => {
  const f = fixture(), first = goal(), admitted = goal("goal-new");
  f.context.seed([first]);
  const refresh = f.context.refreshLongGoals(false);
  f.context.rememberChatGoalSnapshot(admitted);
  f.requests[0].resolve({goals: [first]});
  await refresh;
  assert.equal(f.context.readInventory().find(one => one.goal_id === admitted.goal_id), admitted);
});

test("a fresh authoritative inventory can remove a previously observed missing goal", async () => {
  const f = fixture(), removed = goal();
  f.context.seed([removed]);
  f.context.rememberChatGoalSnapshot(removed);
  const refresh = f.context.refreshLongGoals(false);
  f.requests[0].resolve({goals: []});
  await refresh;
  assert.equal(f.context.readInventory().length, 0);
  assert.equal(f.context.readSelected(), null);
});

test("retention after an admission race expires when the next fresh inventory omits the goal", async () => {
  const f = fixture(), first = goal(), admitted = goal("goal-new");
  f.context.seed([first]);
  const older = f.context.refreshLongGoals(false);
  f.context.rememberChatGoalSnapshot(admitted);
  f.requests[0].resolve({goals: [first]});
  await older;
  assert.equal(f.context.readInventory().find(one => one.goal_id === admitted.goal_id), admitted);
  const fresh = f.context.refreshLongGoals(false);
  f.requests[1].resolve({goals: [first]});
  await fresh;
  assert.equal(f.context.readInventory().some(one => one.goal_id === admitted.goal_id), false);
  assert.equal(f.context.readInventory()[0], first);
});

test("selecting an older same-goal snapshot keeps the newest accepted state", () => {
  const f = fixture(), old = goal(), latest = answered(old);
  f.context.seed([old]);
  f.context.rememberChatGoalSnapshot(latest);
  f.context.selectLongGoalSnapshot(old);
  assertLatest(f, latest);
});

test("an earlier detail load cannot change the user's later goal selection", async () => {
  const f = fixture(), first = goal(), second = goal("goal-b");
  f.context.seed([first, second]);
  const loading = f.context.loadLongGoal(first.goal_id);
  f.context.selectLongGoalSnapshot(second);
  f.requests[0].resolve({goal: answered(first)});
  await loading;
  assert.equal(f.context.readSelected(), second);
  assert.equal(f.requests.length, 1);
});
