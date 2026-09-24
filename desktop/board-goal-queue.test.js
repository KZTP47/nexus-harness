"use strict";

// The legacy paired board-goal queue in the swarm view: one poll timer, the
// Cancel remaining goals button, continuing a queued queue by an explicit
// press, visible failures, and queue sends that leave the user's composer and
// keyboard alone. Everything runs the real app.js functions in a small VM with
// arbitrary portable ids; no provider, server, or saved board is involved.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../src/our_harness/ui/app.js"), "utf8");
function section(start, end) {
  const begin = source.indexOf(start);
  const finish = source.indexOf(end, begin);
  assert.ok(begin >= 0 && finish > begin, start);
  return source.slice(begin, finish);
}
const queueCode = section("function showBoardGoalQueue", "async function workOnEveryBoardGoalLegacy");
const legacyPress = section("async function workOnEveryBoardGoalLegacy", "function missionSelectedGoalId");
const cancelHelper = section("function legacyBoardGoalsCanBeCancelled", "// ---- changing it");
const cancelCode = section("async function cancelLongGoal", "async function stopThemGoing") + cancelHelper;
const pressable = section('  $("swarmCancelGoals").disabled', '  $("swarmStop").disabled');
const flush = async () => { for (let i = 0; i < 20; i += 1) await new Promise(setImmediate); };

function fixture() {
  const state = {requests: [], errors: [], sends: [], confirms: [], missionControls: [],
    timers: new Map(), nextTimer: 1, queues: []};
  const dom = new Map();
  const context = vm.createContext({
    console, state,
    swarmGoalQueue: null, swarmGoalQueueWatching: 0, swarmGoalQueueContinuing: false, swarmGoalQueueMissed: 0,
    swarmGoalWorkRunning: false, SWARM_GOAL_QUEUE_REQUEST_KEY: "portable.goal-queue-request",
    longGoal: null,
    localStorage: {removeItem() {}, getItem() { return null; }, setItem() {}},
    window: {
      setTimeout(callback, wait) { const id = state.nextTimer++; state.timers.set(id, callback); state.lastWait = wait; return id; },
      clearTimeout(id) { state.timers.delete(id); },
      confirm(words) { state.confirms.push(words); return true; },
    },
    $(id) { if (!dom.has(id)) dom.set(id, {textContent: "", disabled: false, title: ""}); return dom.get(id); },
    request: async (url, options = {}) => {
      state.requests.push({url, body: options.body ? JSON.parse(options.body) : null});
      if (url === "/api/swarm/goal-queue") {
        const next = state.queues.length > 1 ? state.queues.shift() : state.queues[0];
        return {queue: next};
      }
      if (url === "/api/swarm/goal-queue/cancel") {
        return {queue: {...context.swarmGoalQueue, status: "cancelled", note: "The remaining board goals were cancelled."}};
      }
      throw new Error(`Unexpected request: ${url}`);
    },
    setWhatCanBePressedInSwarm() {},
    showError(words) { state.errors.push(String(words)); },
    missionControl: async (action) => { state.missionControls.push(action); },
    theSwarmProject(id) { return state.missing ? null : {id, name: "Portable project"}; },
    theSwarmAgent(id) { return {id, name: `Agent ${id}`}; },
    prepareGoalConversation: async () => ({id: "chat-portable"}),
    theChatCardFor() { return state.card; },
    refreshDurableSwarmWorkRecoveries: async () => {},
    workRecoveryFor() { return null; }, rememberWorkRecoveryForKey() {}, renderWorkRecovery() {},
    swarmChatKey(id) { return `${id}:chat-portable`; },
    sendWhatIsTypedTo: async (...args) => {
      state.sends.push(args);
      return state.sendAnswer === undefined ? {said: []} : state.sendAnswer;
    },
    theSwarmBoard() { return {projects: []}; },
  });
  vm.runInContext(queueCode + legacyPress + cancelCode, context);
  state.context = context;
  state.dom = dom;
  state.box = {value: "The user's own unsent words", focus() { state.focused = true; }};
  state.card = {querySelector() { return state.box; }};
  state.item = {id: "item-one", objective: "Make the portable tests pass", project_id: "project-x",
    lead_id: "lead-x", peer_id: "peer-y", lead_name: "Lead", peer_name: "Peer", project_name: "Portable project"};
  state.queue = (status, extra = {}) => ({queue_id: "queue-portable", status, cursor: 0, total: 2,
    completed: 0, current: state.item, ...extra});
  state.fireTimers = async () => {
    const due = [...state.timers.values()];
    state.timers.clear();
    for (const callback of due) callback();
    await flush();
  };
  return state;
}

test("watching a running queue keeps exactly one poll timer and one read per tick", async () => {
  const f = fixture();
  f.queues = [f.queue("running")];
  f.context.watchBoardGoalQueue();
  f.context.watchBoardGoalQueue();
  assert.equal(f.timers.size, 1, "a second call must not start a second watch");
  for (let tick = 1; tick <= 5; tick += 1) {
    const before = f.requests.length;
    await f.fireTimers();
    assert.equal(f.requests.length - before, 1, `tick ${tick} must read the queue once`);
    assert.equal(f.timers.size, 1, `tick ${tick} must leave exactly one timer`);
    assert.notEqual(f.context.swarmGoalQueueWatching, 0);
  }
});

test("the watch stops when the queue finishes and can be started again later", async () => {
  const f = fixture();
  f.queues = [f.queue("running"), f.queue("complete")];
  f.context.watchBoardGoalQueue();
  await f.fireTimers();
  assert.equal(f.timers.size, 1);
  await f.fireTimers();
  assert.equal(f.timers.size, 0);
  assert.equal(f.context.swarmGoalQueueWatching, 0);
  f.queues = [f.queue("running")];
  f.context.watchBoardGoalQueue();
  assert.equal(f.timers.size, 1);
});

test("a watch tick whose read fails does not leave the watch marked as taken", async () => {
  const f = fixture();
  f.queues = [f.queue("running")];
  f.context.watchBoardGoalQueue();
  f.context.request = async () => { throw new Error("The harness is restarting."); };
  await f.fireTimers();
  assert.equal(f.context.swarmGoalQueueWatching, 0);
  assert.equal(f.dom.get("swarmGoalWorkSaid").textContent, "The harness is restarting.");
});

test("a failed read while the queue is running keeps watching, backing off, until it moves on", async () => {
  const f = fixture();
  f.queues = [f.queue("running")];
  f.context.watchBoardGoalQueue();
  await f.fireTimers();
  assert.equal(f.context.swarmGoalQueue.status, "running");
  const working = f.context.request;
  f.context.request = async () => { throw new Error("The harness is restarting."); };
  await f.fireTimers();
  assert.equal(f.timers.size, 1, "the watch is not lost after one failed read");
  assert.equal(f.lastWait, 2400);
  await f.fireTimers();
  assert.equal(f.timers.size, 1);
  assert.equal(f.lastWait, 4800, "and it waits longer after each failure");
  f.context.request = working;
  f.queues = [f.queue("running", {cursor: 1}), f.queue("complete")];
  await f.fireTimers();
  assert.equal(f.lastWait, 1200, "a good read returns to the ordinary pace");
  await f.fireTimers();
  assert.equal(f.timers.size, 0, "and the watch ends when the queue finishes");
  assert.equal(f.context.swarmGoalQueueMissed, 0);
});

test("a failed read while continuing a waiting queue watches again instead of stranding it", async () => {
  const f = fixture();
  f.context.swarmGoalQueue = f.queue("queued");
  const working = f.context.request;
  f.context.request = async () => { throw new Error("The harness is restarting."); };
  await f.context.continueBoardGoalQueue();
  assert.equal(f.timers.size, 1, "one retry is scheduled");
  assert.equal(f.lastWait, 2400);
  assert.equal(f.sends.length, 0);
  f.context.request = working;
  f.queues = [f.queue("complete")];
  await f.fireTimers();
  assert.equal(f.timers.size, 0, "it stops once the queue reads back");
  // A read that fails when no queue was ever seen schedules nothing.
  const g = fixture();
  g.context.request = async () => { throw new Error("offline"); };
  await g.context.continueBoardGoalQueue();
  assert.equal(g.timers.size, 0);
});

test("Cancel remaining goals cancels a waiting legacy queue, not the long-horizon goal", async () => {
  for (const status of ["queued", "paused"]) {
    const f = fixture();
    f.context.longGoal = {goal_id: "long-goal-portable", status: "running"};
    f.context.swarmGoalQueue = f.queue(status);
    await f.context.cancelTheSwarmGoals();
    const cancel = f.requests.find((one) => one.url === "/api/swarm/goal-queue/cancel");
    assert.deepEqual(cancel?.body, {queue_id: "queue-portable"}, status);
    assert.deepEqual(f.missionControls, [], status);
    assert.equal(f.context.swarmGoalQueue.status, "cancelled");
  }
});

test("Cancel remaining goals still cancels the long-horizon goal when no legacy queue waits", async () => {
  for (const legacy of [null, "complete", "cancelled", "running"]) {
    const f = fixture();
    f.context.longGoal = {goal_id: "long-goal-portable", status: "running"};
    f.context.swarmGoalQueue = legacy ? f.queue(legacy) : null;
    await f.context.cancelTheSwarmGoals();
    assert.deepEqual(f.missionControls, ["cancel"], String(legacy));
    assert.equal(f.requests.some((one) => one.url === "/api/swarm/goal-queue/cancel"), false);
  }
  assert.match(source, /\$\("swarmCancelGoals"\)\.addEventListener\("click", cancelTheSwarmGoals\)/);
});

function pressableFor({legacy = null, longGoal = null, workRunning = false} = {}) {
  const dom = new Map();
  const context = vm.createContext({
    held: "", boardGoalPause: "", swarmGoing: false, swarmGoalWorkRunning: workRunning,
    swarmGoalQueue: legacy ? {status: legacy} : null, longGoal, longGoals: longGoal ? [longGoal] : [],
    $(id) { if (!dom.has(id)) dom.set(id, {disabled: false, title: ""}); return dom.get(id); },
  });
  vm.runInContext(cancelHelper
    + "\n" + pressable, context);
  return {cancel: dom.get("swarmCancelGoals"), legacy: dom.get("swarmLegacyGoals")};
}

test("a queued legacy queue can be continued and cancelled by a press, and a running one cannot", () => {
  const queued = pressableFor({legacy: "queued"});
  assert.equal(queued.legacy.disabled, false, "a stranded queued queue must be continuable");
  assert.equal(queued.cancel.disabled, false, "a stranded queued queue must be cancellable");
  const paused = pressableFor({legacy: "paused"});
  assert.equal(paused.cancel.disabled, false);
  const running = pressableFor({legacy: "running"});
  assert.equal(running.legacy.disabled, true);
  assert.equal(running.cancel.disabled, true);
  assert.match(running.cancel.title, /Stop the exact active chat run first/);
  assert.equal(pressableFor({legacy: "queued", workRunning: true}).legacy.disabled, true,
    "a continue that is already sending must not be pressed twice");
  assert.equal(pressableFor().cancel.disabled, true);
  assert.equal(pressableFor({longGoal: {status: "running"}}).cancel.disabled, false);
  assert.equal(pressableFor({longGoal: {status: "complete"}}).cancel.disabled, true);
});

test("reading a queued queue after a reload sends nothing until the user presses continue", async () => {
  const f = fixture();
  // One read on reload, then the press reads it again before and after sending.
  f.queues = [f.queue("queued"), f.queue("queued"), f.queue("queued"), f.queue("complete")];
  await f.context.refreshBoardGoalQueue(false);
  await flush();
  assert.deepEqual(f.sends, [], "a reload is never authority to dispatch provider work");
  assert.equal(f.dom.get("swarmLegacyGoals").textContent, "Continue the saved board goals");
  assert.match(f.dom.get("swarmGoalWorkSaid").textContent, /waiting/);
  await f.context.workOnEveryBoardGoalLegacy();
  assert.equal(f.sends.length, 1, "the user's press continues the exact saved queue");
  assert.deepEqual(JSON.parse(JSON.stringify(f.sends[0][3])),
    {queueId: "queue-portable", itemId: "item-one", text: "Make the portable tests pass"});
});

test("a queued goal is sent with its own words while the user's composer and keyboard stay put", async () => {
  const f = fixture();
  f.queues = [f.queue("queued"), f.queue("complete")];
  await f.context.continueBoardGoalQueue();
  assert.equal(f.sends.length, 1);
  const [agentId, mode, permission, item] = f.sends[0];
  assert.equal(agentId, "lead-x");
  assert.equal(mode, "work");
  assert.equal(permission.boardGoal, true);
  assert.equal(item.text, "Make the portable tests pass");
  assert.equal(f.box.value, "The user's own unsent words");
  assert.equal(f.focused, undefined);
  assert.equal(f.context.swarmGoalWorkRunning, false);
});

test("the queue carries on to the next saved goal after each answer", async () => {
  const f = fixture();
  f.queues = [f.queue("queued"), f.queue("queued", {cursor: 1, current: {...f.item, id: "item-two"}}),
    f.queue("complete")];
  await f.context.continueBoardGoalQueue();
  assert.deepEqual(f.sends.map((one) => one[3].itemId), ["item-one", "item-two"]);
});

test("a queue that cannot continue says why where the goal work is shown", async () => {
  const f = fixture();
  f.missing = true;
  f.queues = [f.queue("queued")];
  const unhandled = [];
  const record = (error) => unhandled.push(error);
  process.on("unhandledRejection", record);
  try {
    await f.context.continueBoardGoalQueue();
    await flush();
  } finally {
    process.off("unhandledRejection", record);
  }
  assert.deepEqual(unhandled, []);
  const said = f.dom.get("swarmGoalWorkSaid").textContent;
  assert.match(said, /The board goals stopped/);
  assert.match(said, /no longer open/);
  assert.doesNotMatch(said, /is working/);
  assert.equal(f.errors.length, 1);
  assert.match(f.errors[0], /no longer open/);
  assert.equal(f.context.swarmGoalWorkRunning, false);
  assert.equal(f.context.swarmGoalQueueContinuing, false);
});

test("the queue opens and creates its chat without taking the keyboard", async () => {
  const opened = [];
  const created = [];
  const context = vm.createContext({
    swarmChats: [{agent: "lead-x", conversations: [], conversation: ""}],
    openTheChatFor: async (id, options) => { opened.push(options); },
    createConversationFor: async (id, peer, scope, options) => {
      created.push({scope, options});
      context.swarmChats[0].conversations = [{id: "chat-new", peer: "peer-y", project: "project-x"}];
      context.swarmChats[0].conversation = "chat-new";
    },
    activateConversationFor: async () => {}, selectConversationProject: async () => {},
    activeConversationFor() {
      return context.swarmChats[0].conversations.find((one) => one.id === context.swarmChats[0].conversation);
    },
  });
  vm.runInContext(section("async function prepareGoalConversation", "function showBoardGoalQueue"), context);
  await context.prepareGoalConversation({id: "project-x", name: "Portable project"},
    {lead: {id: "lead-x", name: "Lead"}, peer: {id: "peer-y", name: "Peer"}});
  assert.deepEqual(JSON.parse(JSON.stringify(opened)), [{focus: false}]);
  assert.deepEqual(JSON.parse(JSON.stringify(created)), [{scope: "", options: {focus: false}}]);

  // openTheChatFor itself: a person's press still focuses, the queue's does not.
  let focused = 0;
  const box = {focus() { focused += 1; }};
  const card = {querySelector() { return box; }, scrollIntoView() {}};
  const chat = vm.createContext({
    swarmChats: [], swarmConversationHydrating: new Set(),
    theSwarmAgent(id) { return {id, at: {x: 40, y: 40}}; },
    renderSwarmBoard() {}, renderTheChatsOnThisBoard() {}, renderTheChatTray() {},
    theChatCardFor() { return card; }, loadConversationsFor: async () => {},
  });
  vm.runInContext(section("async function openTheChatFor", "function closeTheChatFor"), chat);
  await chat.openTheChatFor("lead-x", {focus: false});
  assert.equal(focused, 0);
  await chat.openTheChatFor("lead-x");
  assert.equal(focused, 1);
});
