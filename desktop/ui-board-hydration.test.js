"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../src/our_harness/ui/app.js"), "utf8");
const refresh = source.slice(source.indexOf("async function refreshSwarm"), source.indexOf("function keepTheSwarmPick"));
const summary = source.slice(source.indexOf("function whatTheBoardSays"), source.indexOf("// ---- drawing it"));
function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return {promise, resolve, reject};
}

function fixture() {
  const state = {requests: [], draws: [], errors: [], messages: [], exchange: deferred(), run: deferred(),
    exchangeStarted: deferred(), runStarted: deferred(), status: "Loading your saved board…"};
  const context = vm.createContext({
    swarmNewestRefresh: 0, howManyChangesLanded: 0, swarmBoardHydrated: false,
    swarmSaid: {board: {agents: [], projects: []}}, swarmKept: [], swarmChats: [],
    theBigOne: "", swarmBoardRunId: "", swarmBoardCursor: 0,
    localStorage: {removeItem() {}},
    request(url) {
      const pending = deferred();
      state.requests.push({url, ...pending});
      return pending.promise;
    },
    showProjectAuthorityPause() {}, acceptKeptInventory() {}, keepTheSwarmPick() {},
    renderSwarmBoard() { state.draws.push(context.swarmSaid.board.version); },
    renderSwarmNotReady() {}, renderSwarmPanel() {}, renderTheChatsOnThisBoard() {},
    renderTheKeptBoards() {}, renderTheChatTray() {}, renderTheBigChat() {},
    renderDirectLongGoalBoardRecoveryNotice() {},
    refreshDirectLongGoalRecoveries: async () => {}, refreshDurableSwarmWorkRecoveries: async () => {},
    refreshBoardGoalQueue: async () => {}, refreshLongGoals: async () => {},
    refreshWhatTheySaidToEachOther() { state.exchangeStarted.resolve(); return state.exchange.promise; },
    readSwarmBoardRun() { state.runStarted.resolve(); return state.run.promise; },
    renderWhatTheyAreDoing(doing) { if (doing?.going) context.sayInSwarm("The board is going; pause it before changing projects."); },
    watchWhatTheyAreDoing() {},
    theSwarmBoard() { return context.swarmSaid.board; },
    sayInSwarm(words) { state.status = words; state.messages.push(words); },
    showError(words) { state.errors.push(words); },
  });
  vm.runInContext(refresh + summary, context);
  state.context = context;
  state.read = (quietly = false, options = {}) => context.refreshSwarm(quietly, options);
  state.board = (version) => ({board: {version, agents: [], projects: []}, provider_status_stale: false});
  state.finish = () => { state.exchange.resolve(); state.run.resolve(null); };
  return state;
}

for (const quietly of [false, true]) {
  test(`first board hydration publishes readiness before auxiliary reads finish (quiet: ${quietly})`, async () => {
    const f = fixture();
    const reading = f.read(quietly);
    assert.equal(f.status, "Loading your saved board…");
    assert.equal(f.requests[0].url, "/api/swarm?refresh_providers=false");
    f.requests[0].resolve(f.board("first-local-board"));
    await f.exchangeStarted.promise;
    assert.equal(f.context.swarmBoardHydrated, true);
    assert.deepEqual(f.draws, ["first-local-board"]);
    assert.equal(f.status, "Nothing on the board yet. Press Add another agent to get started.");
    f.exchange.resolve();
    await f.runStarted.promise;
    assert.doesNotMatch(f.status, /Loading/);
    f.run.resolve({going: true, next_cursor: 3});
    await reading;
    assert.equal(f.status, "The board is going; pause it before changing projects.",
      "the local summary must not overwrite the later authoritative running state");
    assert.equal(f.context.swarmBoardCursor, 3);
  });
}

test("a quiet first hydration that overtakes navigation clears loading and rejects the obsolete response", async () => {
  const f = fixture();
  const navigation = f.read();
  const background = f.read(true);
  f.requests[1].resolve(f.board("current-quiet-board"));
  await f.exchangeStarted.promise;
  assert.doesNotMatch(f.status, /Loading/);
  const accepted = f.status;
  f.requests[0].resolve(f.board("obsolete-navigation-board"));
  await navigation;
  assert.deepEqual(f.draws, ["current-quiet-board"]);
  assert.equal(f.status, accepted);
  f.finish();
  await background;
  assert.equal(f.status, accepted);
});

test("later quiet refresh and delayed history preserve the user's current action message", async () => {
  const f = fixture();
  f.finish();
  const first = f.read();
  f.requests[0].resolve(f.board("initial"));
  await first;
  f.context.sayInSwarm("Your exact project checks were approved.");
  const later = f.read(true);
  assert.equal(f.requests[1].url, "/api/swarm");
  f.requests[1].resolve(f.board("provider-status-updated"));
  await later;
  assert.equal(f.status, "Your exact project checks were approved.");
});

test("a board mutation during the read prevents stale rendering or readiness publication", async () => {
  const f = fixture();
  const reading = f.read(true);
  f.context.howManyChangesLanded += 1;
  f.context.sayInSwarm("Saved the new board arrangement.");
  f.requests[0].resolve(f.board("pre-mutation-board"));
  await reading;
  assert.equal(f.context.swarmBoardHydrated, false);
  assert.deepEqual(f.draws, []);
  assert.equal(f.status, "Saved the new board arrangement.");
});

test("failed local hydration shows the actual error without claiming readiness or dispatching another read", async () => {
  const f = fixture();
  const reading = f.read(true);
  f.requests[0].reject(new Error("The saved board could not be read."));
  await reading;
  assert.equal(f.context.swarmBoardHydrated, false);
  assert.deepEqual(f.draws, []);
  assert.equal(f.requests.length, 1);
  assert.deepEqual(f.errors, ["The saved board could not be read."]);
  assert.equal(f.status, "The saved board could not be read.");
});

test("an auxiliary failure after local hydration remains visible", async () => {
  const f = fixture();
  const reading = f.read();
  f.requests[0].resolve(f.board("readable-board"));
  await f.exchangeStarted.promise;
  assert.doesNotMatch(f.status, /Loading/);
  f.exchange.resolve();
  await f.runStarted.promise;
  f.run.reject(new Error("The run journal could not be read."));
  await reading;
  assert.equal(f.context.swarmBoardHydrated, true);
  assert.equal(f.status, "The run journal could not be read.");
  assert.deepEqual(f.errors, ["The run journal could not be read."]);
});

function section(start, end) {
  const begin = source.indexOf(start);
  const finish = source.indexOf(end, begin);
  assert.ok(begin >= 0 && finish > begin, start);
  return source.slice(begin, finish);
}

test("opening a saved board is not drawn over by a slower board read that was already on its way", async () => {
  const f = fixture();
  f.context.window = {confirm() { return true; }};
  f.context.loadConversationsFor = async () => {};
  vm.runInContext(section("async function openTheKeptBoard", "async function forgetTheKeptBoard"), f.context);
  f.finish();
  const slowRead = f.read(true);
  const opening = f.context.openTheKeptBoard("arbitrary saved board");
  assert.equal(f.requests[1].url, "/api/swarm/open-kept");
  f.requests[1].resolve(f.board("opened-saved-board"));
  await opening;
  f.requests[0].resolve(f.board("board-before-opening"));
  await slowRead;
  assert.deepEqual(f.draws, ["opened-saved-board"]);
  assert.equal(f.context.swarmSaid.board.version, "opened-saved-board");
  assert.equal(f.status, "Opened the board saved as arbitrary saved board.");
});

function runWatchFixture() {
  const state = {timers: new Map(), nextTimer: 1, asks: 0, answers: [], rendered: [], refreshed: 0, said: {}};
  const context = vm.createContext({
    state, swarmWatching: 0, swarmBoardRunId: "run-portable", swarmBoardCursor: 0,
    swarmBoardRequestId: "request-portable", swarmDoing: null, swarmChats: [],
    localStorage: {removeItem() {}},
    window: {
      setTimeout(callback, wait) { const id = state.nextTimer++; state.timers.set(id, {callback, wait}); return id; },
      clearTimeout(id) { state.timers.delete(id); },
      setInterval() { throw new Error("The run watch must not use a free-running interval."); },
    },
    $(id) { if (!state.said[id]) state.said[id] = {textContent: ""}; return state.said[id]; },
    readSwarmBoardRun() {
      state.asks += 1;
      const next = state.answers.shift();
      if (next instanceof Error) return Promise.reject(next);
      if (next === "hang") return new Promise(() => {});
      return Promise.resolve(next);
    },
    renderWhatTheyAreDoing(doing) { state.rendered.push(doing); },
    refreshTheChatFor() {}, refreshWhatTheySaidToEachOther() {},
    refreshSwarm() { state.refreshed += 1; },
  });
  vm.runInContext(section("const SWARM_WATCH_EVERY_MS", "// What the run last said it was doing"), context);
  state.context = context;
  state.fire = async () => {
    const due = [...state.timers.values()];
    state.timers.clear();
    for (const one of due) one.callback();
    for (let i = 0; i < 10; i += 1) await new Promise(setImmediate);
    return due.map((one) => one.wait);
  };
  return state;
}

test("a failed run read is asked again with a growing, bounded wait instead of giving up", async () => {
  const w = runWatchFixture();
  const lost = new Error("The harness did not answer.");
  w.answers = [lost, lost, lost, lost, lost, lost, lost, {going: true, next_cursor: 4}, {going: false, note: "Done"}];
  w.context.watchWhatTheyAreDoing();
  const waits = [];
  for (let tick = 0; tick < 9; tick += 1) {
    assert.equal(w.timers.size, 1, `tick ${tick} must have exactly one pending ask`);
    waits.push(...await w.fire());
    if (tick < 7) assert.match(w.said.swarmDoingSaid.textContent, /did not answer.*ask again/);
  }
  assert.deepEqual(waits, [1500, 3000, 6000, 12000, 24000, 30000, 30000, 30000, 1500]);
  assert.equal(w.asks, 9);
  assert.equal(w.context.swarmBoardCursor, 4);
  assert.equal(w.timers.size, 0, "a finished run stops the watch");
  assert.equal(w.context.swarmWatching, 0);
  assert.equal(w.refreshed, 1, "a finished run releases the board through a fresh read");
  assert.deepEqual(w.rendered.at(-1), {going: false, note: "Done"});
});

test("a slow run read is never overlapped by another ask or a second watch", async () => {
  const w = runWatchFixture();
  w.answers = ["hang"];
  w.context.watchWhatTheyAreDoing();
  await w.fire();
  assert.equal(w.asks, 1);
  assert.equal(w.timers.size, 0, "no further ask is scheduled while one is on its way");
  w.context.watchWhatTheyAreDoing();
  assert.equal(w.timers.size, 0, "a second call must not start a watch beside the one waiting");
  assert.notEqual(w.context.swarmWatching, 0);
});

function errorFixture(swarmShowing) {
  const nodes = new Map();
  const node = (id) => {
    if (!nodes.has(id)) nodes.set(id, {id, textContent: "", className: "", hidden: false});
    return nodes.get(id);
  };
  node("swarmView").hidden = !swarmShowing;
  node("swarmSaid").textContent = "Your board is as you left it.";
  const context = vm.createContext({
    announced: [], events: [],
    $: node, announce(words) { context.announced.push(words); },
    appendEvent(kind, words) { context.events.push([kind, words]); },
  });
  vm.runInContext(section("function showError(message)", "async function startRun")
    + section("function sayInSwarm", "\n") + "\n", context);
  return {context, node};
}

test("an error raised on the swarm board is shown on the board's own status line", () => {
  const {context, node} = errorFixture(true);
  context.showError("Nexus could not open the composer for Lead.");
  assert.equal(node("swarmSaid").textContent, "Nexus could not open the composer for Lead.");
  assert.equal(node("validationStatus").textContent, "Nexus could not open the composer for Lead.");
  assert.deepEqual(context.announced, ["Error: Nexus could not open the composer for Lead."]);
});

test("an error on another view leaves the swarm status line as it was", () => {
  const {context, node} = errorFixture(false);
  context.showError("This workflow could not be validated.");
  assert.equal(node("validationStatus").textContent, "This workflow could not be validated.");
  assert.equal(node("validationStatus").className, "status-fail");
  assert.equal(node("swarmSaid").textContent, "Your board is as you left it.");
});
