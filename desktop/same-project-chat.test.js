"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const fixtureRoot = path.join(require("node:os").tmpdir(), "nexus-same-project-ui-fixture");

const source = fs.readFileSync(path.join(__dirname, "../src/our_harness/ui/app.js"), "utf8");
function section(start, end) {
  const from = source.indexOf(start), to = source.indexOf(end, from);
  assert.ok(from >= 0 && to > from, start);
  return source.slice(from, to);
}
function node(tag, className = "", words = "") {
  return {
    tag, className, words, children: [], dataset: {}, replacements: 0,
    append(...children) { this.children.push(...children); },
    replaceChildren() { this.children = []; this.replacements += 1; },
    addEventListener() {},
    get textContent() { return [this.words, ...this.children.map(one => one.textContent)].join(" "); },
  };
}
function fixture() {
  const context = vm.createContext({
    make: node, theSwarmAgent: () => null, openChatGoalDetails() {},
    activeConversationFor: () => context.conversation,
  });
  vm.runInContext(
    section("function chatGoalParticipants", "function chatGoalBinding")
    + section("function fillChatGoalPanel", "function syncChatGoalControls")
    + section("function chatGoalActivity", "function renderSwarmChatActivity")
    + section("function longHorizonAdmissionWords", "function finishLongHorizonAdmissionActivity")
    + section("function longHorizonStateWords", "function longHorizonAssignmentsForAgent"), context);
  return context;
}
function goal(id = "goal-a", chat = "chat-a") {
  return {
    goal_id: id, conversation_id: chat, project: {id: "one-project"}, status: "running",
    lead_agent_id: "builder", requested_agent_ids: ["builder", "reviewer"],
    agents: [{id: "builder", name: "Builder"}, {id: "reviewer", name: "Reviewer"}],
    tasks: [{id: "task-a", state: "running", assigned_agent_id: "builder", provider_effect_state: "dispatched"}],
    execution_workspace: {path: `workspaces/${id}`}, workspace_publication: {state: "pending"},
  };
}

test("same-project saved chats retain their own concurrent goal and provider activity", () => {
  const f = fixture();
  const first = goal(), second = goal("goal-b", "chat-b");
  f.longGoals = [first, second];
  for (const current of [first, second]) {
    f.conversation = {id: current.conversation_id, project: "one-project", pair: ["builder", "reviewer"]};
    const found = f.chatLongGoalContext("builder");
    assert.equal(found.goal, current);
    assert.equal(found.problem, "");
    const activity = f.chatGoalActivity(found);
    assert.equal(activity.stage, "Builder is responding");
    assert.match(activity.detail, /independent working copy/);
    assert.match(activity.detail, /after checks and conflict review/);
    assert.doesNotMatch(activity.detail, /another saved goal|project access/i);
  }
});

test("publication stage and exact conflict paths are visible without claiming completion", () => {
  const f = fixture(), current = goal();
  current.workspace_publication = {state: "publishing"};
  let activity = f.chatGoalActivity({goal: current, problem: ""});
  assert.equal(activity.stage, "Applying checked results");
  assert.match(activity.detail, /Other teams keep their independent working copies/);
  current.status = "paused";
  current.workspace_path = path.join(fixtureRoot, "goal-a", "project");
  current.workspace_publication = {state: "conflict", message: "The saved result is retained.",
    conflicts: ["src/shared.js", {path: "assets/logo.png"}]};
  activity = f.chatGoalActivity({goal: current, problem: ""});
  assert.equal(activity.state, "attention");
  assert.equal(activity.stage, "Project changes need reconciliation");
  assert.match(activity.detail, /src\/shared\.js, assets\/logo\.png/);
  assert.match(activity.detail, /result is retained/);
  assert.ok(activity.detail.includes(`Retained working copy: ${current.workspace_path}`));
  assert.match(activity.detail, /Reconcile.*in the project.*Resume team/);
  assert.doesNotMatch(activity.detail, /completed|verified complete/);
});

test("publication-only changes refresh the exact chat panel while identical polls preserve its controls", () => {
  const f = fixture(), current = goal(), panel = node("section");
  const render = () => f.fillChatGoalPanel(panel, "builder", {goal: current, problem: ""});
  render();
  assert.match(panel.textContent, /independent working copy/);
  const original = panel.replacements;
  current.revision = 10;
  render();
  assert.equal(panel.replacements, original);
  current.workspace_publication = {state: "publishing"};
  render();
  assert.equal(panel.replacements, original + 1);
  assert.match(panel.textContent, /Applying checked results/);
  current.workspace_publication = {state: "conflict", conflicts: ["src/same.js"]};
  render();
  assert.equal(panel.replacements, original + 2);
  assert.match(panel.textContent, /src\/same\.js/);
  assert.match(panel.textContent, /Resume team/);
});

test("an unpublished isolated result cannot produce a verified completion admission receipt", () => {
  const f = fixture(), current = goal();
  current.status = "complete";
  for (const state of ["pending", "publishing", "conflict"]) {
    current.workspace_publication = {state};
    const words = f.longHorizonAdmissionWords(current);
    assert.equal(words.stage, "Result awaiting publication");
    assert.doesNotMatch(words.detail, /verified completion/);
  }
  current.workspace_publication = {state: "published"};
  assert.equal(f.longHorizonAdmissionWords(current).stage, "Goal verified complete");
});

test("new isolated admissions explain working copies and legacy project waits remain truthful", () => {
  const f = fixture(), current = goal();
  for (const status of ["queued", "running"]) {
    current.status = status;
    assert.match(f.longHorizonAdmissionWords(current).detail, /independent working copy/);
  }
  delete current.execution_workspace;
  delete current.workspace_publication;
  current.status = "waiting_for_project";
  const activity = f.chatGoalActivity({goal: current, problem: ""});
  assert.equal(activity.stage, "Waiting for project access");
  assert.match(activity.detail, /Another saved goal is using this project/);
  assert.match(f.longHorizonAdmissionWords(current).detail, /current project owner releases it/);
});

for (const approve of [false, true]) {
  test(`snapshot test commands require exact-goal preview and explicit confirmation (approve: ${approve})`, async () => {
    const commands = [["python", "-m", "unittest", "discover"]];
    const calls = [], dialogs = [], errors = [], snapshots = [];
    const exactGoal = {...goal(), status: "paused"};
    const button = node("button");
    const f = vm.createContext({
      window: {confirm: message => { dialogs.push(message); return approve; }},
      showError: message => errors.push(message),
      rememberChatGoalSnapshot: current => snapshots.push(current), refreshLongGoals: async () => {},
      request: async (url, options) => {
        calls.push({url, body: options?.body ? JSON.parse(options.body) : null});
        return options ? {goal: exactGoal, approval: {approved: true}}
          : {goal_id: exactGoal.goal_id, revision: 7, project_path: path.join(fixtureRoot, "selected project"),
            commands, approval_digest: "a".repeat(64), approved: false, can_approve: true};
      },
    });
    vm.runInContext(section("async function reviewGoalVerificationCommands", "function fillChatGoalPanel"), f);
    await f.reviewGoalVerificationCommands(exactGoal, button);
    assert.equal(calls[0].url, "/api/long-horizon/verification-approval?goal_id=goal-a");
    assert.equal(dialogs.length, 1);
    assert.ok(dialogs[0].includes(JSON.stringify(commands[0])));
    assert.ok(dialogs[0].includes(path.join(fixtureRoot, "selected project")));
    assert.ok(dialogs[0].includes("a".repeat(64)));
    assert.match(dialogs[0], /only to this chat's goal/);
    assert.equal(calls.length, approve ? 2 : 1);
    assert.equal(snapshots.length, approve ? 1 : 0);
    if (approve) assert.deepEqual(calls[1].body, {
      goal_id: "goal-a", expected_revision: 7, command_digest: "a".repeat(64), approved: true,
    });
    assert.equal(calls.some(one => /control|resume|swarm\/verification-approval/.test(one.url)), false);
    assert.equal(button.disabled, false);
    assert.deepEqual(errors, []);
  });
}

test("a mismatched snapshot approval preview cannot request confirmation or submit", async () => {
  let calls = 0, dialogs = 0;
  const errors = [];
  const f = vm.createContext({
    window: {confirm() { dialogs += 1; return true; }}, showError: value => errors.push(value),
    request: async () => { calls += 1; return {goal_id: "different-goal", revision: 2, can_approve: true}; },
  });
  vm.runInContext(section("async function reviewGoalVerificationCommands", "function fillChatGoalPanel"), f);
  await f.reviewGoalVerificationCommands(goal(), node("button"));
  assert.equal(calls, 1);
  assert.equal(dialogs, 0);
  assert.match(errors[0], /exact goal/);
});
