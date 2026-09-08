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
  const lead = {id: "shared-builder", name: "Builder", ready: true};
  const chats = ["red", "blue", "third"].map(id => ({
    id, name: id, project: `project-${id}`, pair: [lead.id, `peer-${id}`],
    pair_agents: [lead, {id: `peer-${id}`, name: "Reviewer", ready: true}],
  }));
  const held = {agent: lead.id, conversation: "red", conversations: chats, said: [], saidFor: "red"};
  const nodes = new Map(), requests = [], notices = [];
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {
      id, disabled: false, value: "", textContent: "", dataset: {}, checked: false,
      focus() { this.focused = !this.disabled; },
      querySelectorAll() { return []; }, querySelector() { return null; },
    });
    return nodes.get(id);
  };
  const compact = {dataset: {agent: lead.id}, querySelector: id => id === ".chat-destination" ? null : node(id)};
  const navigation = {dataset: {conversationAction: "pick", chatId: "third"}};
  node("theBigChatConversationList").querySelectorAll = () => [navigation];
  node("swarmBoard").querySelectorAll = () => [compact];
  const context = vm.createContext({
    console, AbortController, lead, held, swarmChats: [held], theBigOne: lead.id,
    swarmBusy: new Set(["chat:red"]), swarmStopping: new Set(), swarmChatResetting: new Set(),
    swarmConversationSwitching: new Set(), swarmConversationHydrating: new Set(),
    swarmConversationTranscriptRefreshes: new Set(), swarmConversationListRevisions: new Map(),
    swarmConversationListControllers: new Map(), swarmConversationTranscriptControllers: new Map(),
    swarmChatRevisions: new Map(), swarmChatLimits: new Map(), swarmAgentSettingDrafts: new Map(),
    swarmChatAttachmentLoads: new Map(),
    swarmGoing: false, swarmGoalWorkRunning: false, swarmGoalQueue: null,
    longGoal: null, longGoals: [{conversation_id: "red", status: "running"}],
    swarmSaid: {}, directLongGoalRecoveryInventoryReady: true, directLongGoalRecoveryError: "",
    $: node, window: {confirm: () => true},
    theSwarmAgent: () => lead, thePickedAgent: () => lead, thePickedProject: () => null,
    thePickedLine: () => null, theSwarmBoard: () => ({agents: [lead], projects: []}),
    theChatCardFor: () => compact, whyTheBoardIsHeld: () => "Another goal is running",
    boardGoalAuthorityPause: () => "", safeAgentPicture: () => null,
    renderSwarmAgentSaveState() {}, syncChatRoundPolicy() {}, renderWorkRecoveryButtons() {},
    syncChatGoalControls() {}, renderTheBigChat() {}, renderTheChatThreadFor() {},
    syncChatRecipientWords: () => ({direct: "Ask builder", team: "Ask both", expected: 2}),
    syncChatTeamReadiness: () => [], workRecoveryFor: () => null,
    setSwarmProjectWorkControl(button, disabled) { button.disabled = disabled; },
    rememberSwarmChatComposer() {}, syncSwarmChatComposer() {},
    transcriptIdentityFor: () => held.conversation,
    activeConversationIdFor: () => held.conversation,
    bigChatShows: (_agent, id = held.conversation) => id === held.conversation,
    keepWhatWasSaidTo(_agent, said, id = held.conversation) { held.said = said; held.saidFor = id; },
    countWhatIsTypedTo() {}, conversationReadWasCancelled: error => error.name === "AbortError",
    sayInBigChatConversationFor(_agent, text) { notices.push(text); },
    sayInTheChatFor(_agent, text) { notices.push(text); },
    request(url, options) { const pending = deferred(); requests.push({url, options, ...pending}); return pending.promise; },
  });
  vm.runInContext(
    section("function activeConversationFor", "// A project goal belongs")
    + section("function swarmChatKeyFor", "function swarmChatActivityFor")
    + section("function nextConversationListRevision", "function cancelConversationReadLane")
    + section("function setWhatCanBePressedInSwarm", "// ---- changing it")
    + section("function applyConversationList", "async function archiveConversationFor")
    + section("async function archiveConversationFor", "async function selectConversationProject")
    + section("function setWhatCanBePressedInAChat", "function stoppedChatError")
    + section("async function refreshTheChatFor", "async function copyChatCode"), context);
  return {context, held, chats, requests, notices, node, navigation,
    run: text => vm.runInContext(text, context),
    selection: active => ({active, chats}),
    assertInteractive() {
      for (const id of ["theBigChatBox", "theBigChatWork", ".swarm-chat-box", ".swarm-chat-work"]) {
        assert.equal(node(id).disabled, false, `${id} must be usable while history loads`);
      }
      assert.equal(navigation.disabled, false, "another chat must remain reachable");
      assert.equal(context.swarmBusy.has("chat:red"), true, "the other run must remain owned");
    },
  };
}

for (const operation of ["activate", "create", "restore", "archive"]) {
  test(`${operation}: confirmed chat identity unlocks both composers before slow history finishes`, async () => {
    const f = fixture();
    if (operation === "restore") f.chats[1].archived_at = "earlier";
    if (operation === "archive") f.context.swarmBusy.clear();
    const call = operation === "activate" ? "activateConversationFor(lead.id, 'blue')"
      : operation === "create" ? "createConversationFor(lead.id, 'peer-blue')"
      : operation === "restore" ? "restoreConversationFor(lead.id, 'blue')"
      : "archiveConversationFor(lead.id, 'red')";
    const changing = f.run(call);
    assert.equal(f.node("theBigChatBox").disabled, true, "unknown mutation outcome must stay fenced");
    assert.equal(f.navigation.disabled, true);
    delete f.chats[1].archived_at;
    f.requests[0].resolve(f.selection("blue"));
    await tick();
    assert.equal(f.requests.length, 2, "history refresh must still start");
    assert.match(f.requests[1].url, /swarm\/said.*chat=blue/);
    if (operation === "archive") f.context.swarmBusy.add("chat:red");
    f.assertInteractive();
    await changing;
    f.node("theBigChatBox").value = "Start an independent goal here";
    f.requests[1].resolve({said: [{text: "saved blue history"}]});
    await tick();
    assert.equal(f.node("theBigChatBox").value, "Start an independent goal here");
    assert.equal(f.held.said[0].text, "saved blue history");
    f.assertInteractive();
  });
}

test("a late transcript cannot overwrite the next selected chat or clear its navigation lease", async () => {
  const f = fixture();
  const first = f.run("activateConversationFor(lead.id, 'blue')");
  f.requests[0].resolve(f.selection("blue"));
  await tick();
  f.assertInteractive();
  await first;
  const second = f.run("activateConversationFor(lead.id, 'third')");
  f.requests[1].resolve({said: [{text: "late blue history"}]});
  await tick();
  assert.equal(f.node("theBigChatBox").disabled, true, "older refresh cannot release newer mutation");
  assert.equal(f.held.conversation, "third");
  assert.deepEqual(Array.from(f.held.said), []);
  f.requests[2].resolve(f.selection("third"));
  await tick();
  await second;
  f.requests[3].resolve({said: [{text: "third history"}]});
  await tick();
  assert.equal(f.held.said[0].text, "third history");
  f.assertInteractive();
});

test("a failed background history read reports the error without freezing confirmed navigation", async () => {
  const f = fixture();
  const changing = f.run("activateConversationFor(lead.id, 'blue')");
  f.requests[0].resolve(f.selection("blue"));
  await tick();
  f.assertInteractive();
  await changing;
  f.requests[1].reject(new Error("History is temporarily unavailable"));
  await tick();
  assert.ok(f.notices.includes("History is temporarily unavailable"));
  f.assertInteractive();
});

test("first chat hydration enables the confirmed identity before its history arrives", async () => {
  const f = fixture();
  f.held.conversation = "";
  f.context.swarmConversationHydrating.add(f.held.agent);
  f.run("setWhatCanBePressedInSwarm()");
  assert.equal(f.node("theBigChatBox").disabled, true);
  const loading = f.run("loadConversationsFor(lead.id)");
  f.requests[0].resolve(f.selection("blue"));
  await tick();
  assert.match(f.requests[1].url, /swarm\/said.*chat=blue/);
  f.assertInteractive();
  f.requests[1].resolve({said: [{text: "saved history"}]});
  await loading;
  assert.equal(f.held.said[0].text, "saved history");
});


test("selected history installs diagnostics and metadata refresh preserves them", async () => {
  const f = fixture();
  const problem = {code: "collaboration_record_untrusted", action: "reset_collaboration_record"};
  const loading = f.run("loadConversationsFor(lead.id)");
  f.requests[0].resolve(f.selection("blue"));
  await tick();
  f.requests[1].resolve({said: [{text: "Saved blue"}], conversation: {
    id: "blue", collaboration_checked: true, collaboration_problem: problem,
  }});
  await loading;
  assert.equal(f.held.conversations.find(one => one.id === "blue").collaboration_problem, problem);
  const refresh = f.run("loadConversationsFor(lead.id, false)");
  f.requests[2].resolve({active: "blue", chats: f.chats.map(one => ({
    ...one, collaboration_checked: false, collaboration_problem: null,
  }))});
  await refresh;
  assert.equal(f.held.conversations.find(one => one.id === "blue").collaboration_problem, problem);
  assert.equal(f.held.said[0].text, "Saved blue");
});

test("late history diagnostics cannot contaminate the next selected conversation", async () => {
  const f = fixture();
  const first = f.run("activateConversationFor(lead.id, 'blue')");
  f.requests[0].resolve(f.selection("blue"));
  await tick();
  await first;
  const second = f.run("activateConversationFor(lead.id, 'third')");
  f.requests[1].resolve({said: [{text: "Late blue"}], conversation: {
    id: "blue", collaboration_problem: {code: "collaboration_record_untrusted"},
  }});
  await tick();
  assert.equal(f.held.conversations.find(one => one.id === "third").collaboration_problem, undefined);
  f.requests[2].resolve(f.selection("third"));
  await tick();
  await second;
  f.requests[3].resolve({said: []});
  await tick();
});

test("sidebar distinguishes initial loading from a verified empty inventory", () => {
  const f = fixture();
  const made = [];
  f.context.make = (_tag, _class, text = "") => {
    const node = {textContent: text, children: [], dataset: {},
      append(...children) { this.children.push(...children); },
      addEventListener() {}, classList: {toggle() {}}, setAttribute() {},
    };
    made.push(node);
    return node;
  };
  f.context.anAgentFace = () => ({});
  f.context.conversationPairsFor = () => [[f.held.agent]];
  f.context.connectedPairsFor = () => [];
  f.context.pairKey = pair => pair.join("|");
  const list = f.node("theBigChatConversationList");
  list.replaceChildren = () => {};
  list.append = () => { list.childElementCount = 1; };
  f.held.conversations = [];
  f.context.swarmConversationHydrating.add(f.held.agent);
  vm.runInContext(section("function renderTheConversationSidebar", "function renderTheConversationProject"), f.context);
  f.run("renderTheConversationSidebar(held.agent)");
  assert.ok(made.some(one => one.textContent === "Loading saved chats…"));
  assert.ok(!made.some(one => one.textContent.startsWith("No saved chats")));
  made.length = 0;
  f.context.swarmConversationHydrating.clear();
  f.run("renderTheConversationSidebar(held.agent)");
  assert.ok(made.some(one => one.textContent === "No saved chats for this agent."));
});
