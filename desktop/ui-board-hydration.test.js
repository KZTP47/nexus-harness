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
