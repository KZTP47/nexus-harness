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
    tag, className, words, children: [], dataset: {}, style: {removeProperty() {}, setProperty() {}}, replacements: 0,
    classList: {toggle() {}}, setAttribute() {},
    querySelector(selector) {
      const nodes = this.children.flatMap(one => [one, ...(one.descendants?.() || [])]);
      return nodes.find(one => selector.startsWith('.') ? one.className.split(' ').includes(selector.slice(1)) : one.tag === selector) || null;
    },
    descendants() { return this.children.flatMap(one => [one, ...(one.descendants?.() || [])]); },
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
    appendGoalAccessControls() {}, chatGoalBinding: () => ({}), swarmChatKey: () => 'fixture-chat',
  });
  vm.runInContext(
    section("function facilitatorCompletionDetail", "function goalReviewer")
    + section("function chatGoalParticipants", "function chatGoalBinding")
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
    assert.equal(activity.stage, "Waiting for Builder");
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
  current.verification = {status: "passed"};
  assert.equal(f.longHorizonAdmissionWords(current).stage, "Goal verified complete");
});

test("a goal the agents agreed was done is complete but never called verified", () => {
  const f = fixture(), current = goal();
  current.status = "complete";
  current.workspace_publication = {state: "published"};
  for (const verification of [
    {status: "not_configured", reason: "No project tests were configured; no tests ran."},
    {status: "not_run"}, undefined,
  ]) {
    current.verification = verification;
    const words = f.longHorizonAdmissionWords(current);
    assert.equal(words.stage, "Goal complete", JSON.stringify(verification));
    assert.doesNotMatch(`${words.stage} ${words.detail}`, /verified/i);
    assert.match(words.detail, /Agents agreed it is done; no automatic check was available/);
  }
  // A board-work result names how it was found done the same way.
  assert.equal(f.goalAutomaticChecksPassed({verification_status: "deterministically_verified"}), true);
  assert.equal(f.goalAutomaticChecksPassed({verification_status: "agent_verified", machine_verified: true}), false);
  assert.equal(f.goalAutomaticChecksPassed({machine_verified: false, verification: {status: "passed"}}), false);
});

test("goal notes explain what happened in plain words and never read as a failure", () => {
  const f = fixture();
  assert.equal(f.goalResultNotices(null).length, 0);
  assert.equal(f.goalResultNotices({status: "complete"}).length, 0);
  const notes = f.goalResultNotices({
    execution_workspace: {skipped_links: {count: 3, paths: ["a", "b", "c"]}},
    tasks: [{closeout_outcome: {evidence_notes: ["The judge approved without a recognised evidence reference for: docs updated"]}}],
    provider_setup_status: {changed: false, refresh_pending: true},
    agent_access: {mode: "ask", grants: {one: {decision: "deny", commands: [["rm", "-rf", "x"]]}, two: {decision: "allow"}}},
    verification: {
      requirement_contract: {
        requirements: [{id: "path_1", description: "The goal names src/app.js (hint: it may need a change)"}],
        planned_effect_paths: ["src/app.js"],
      },
      requirement_evidence: {artifacts: {advisory_unmet: ["path_1"]}, execution: {advisory_unmet: ["path_1", "behavior"]}},
    },
  }, [{type: "closeout_repeated_findings", payload: {previous_rejections: 4}}]);
  const said = notes.join("\n");
  assert.match(said, /3 linked files were left out of the private copy\./);
  assert.match(said, /judge approved without a recognised evidence reference for: docs updated\./);
  assert.match(said, /same submission 4 times; the agents keep working/);
  assert.match(said, /Only provider settings .* changed/);
  assert.match(said, /You denied 1 command request; the agents were told not to run it/);
  assert.match(said, /Not required, but not done: The goal names src\/app\.js \(hint: it may need a change\); behavior\./);
  assert.match(said, /^Hint: the goal's wording mentions src\/app\.js; the agents decide what to change\.$/m);
  assert.doesNotMatch(said, /fail|error|blocked/i);
  assert.match(f.goalResultNotices({skipped_links: {count: 1}})[0], /^1 linked file was left out/);
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
  assert.equal(activity.stage, "Waiting for legacy project work");
  assert.match(activity.detail, /An older exclusive run is using this project/);
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

test("a goal whose provider route changed offers the chat's reconnect review, not only a new goal", () => {
  const f = fixture();
  const current = goal();
  current.status = "paused";
  f.conversation = {id: "chat-a", project: "one-project", pair: ["builder", "reviewer"]};
  f.longGoals = [current];
  const plain = f.chatLongGoalContext("builder");
  assert.equal(plain.reconnectChat, undefined, "a current goal offers no reconnect");
  for (const drift of [
    {provider_setup_changed: true, provider_setup_status: {code: "provider_setup_changed",
      recovery_action: "start_new_goal_with_current_setup", message: "The saved provider setup changed.",
      agents: [{agent_id: "builder", code: "route_identity_changed"}]}},
    {provider_setup_status: {code: "current", agents: [{agent_id: "reviewer", code: "route_identity_changed"}]}},
  ]) {
    Object.assign(current, {provider_setup_changed: false, provider_setup_status: {}}, drift);
    const found = f.chatLongGoalContext("builder");
    assert.equal(found.reconnectChat, f.conversation, JSON.stringify(drift));
    assert.equal(found.reconnectForGoal, true);
    assert.equal(found.repairChat, undefined, "the chat itself has no binding problem");
  }
  // A goal from another team setup is never offered this chat's reconnect.
  current.requested_agent_ids = ["builder", "someone-else"];
  assert.equal(f.chatLongGoalContext("builder").reconnectChat, undefined);
  // The chat's own reviewable problem keeps its existing path.
  current.requested_agent_ids = ["builder", "reviewer"];
  f.conversation.binding_problem = {can_review_reconnect: true, message: "Provider changed."};
  const own = f.chatLongGoalContext("builder");
  assert.equal(own.reconnectChat, f.conversation);
  assert.equal(own.reconnectForGoal, undefined);
});

test("Mission control shows how a finished goal was found done, with its notes, beside the recorded checks", () => {
  const f = fixture();
  const evidence = node("section");
  Object.assign(f, {
    $: (id) => (id === "missionEvidence" ? evidence : node("div")),
    longGoalEvents: [], locationOpenButton: () => node("button"), appendFacilitatorRecovery() {},
    rememberChatGoalSnapshot() {}, refreshLongGoals: async () => {},
  });
  vm.runInContext(section("function goalAutomaticChecksPassed", "function finishLongHorizonAdmissionActivity")
    + "\nfunction renderEvidence() {\n"
    + section('  const evidence = $("missionEvidence");', "  renderMissionEvents();") + "}\n", f);
  f.longGoal = {goal_id: "g", status: "complete", verification: {status: "not_configured"},
    execution_workspace: {skipped_links: {count: 2}}};
  f.renderEvidence();
  assert.match(evidence.textContent, /Done\. Agents agreed it is done; no automatic check was available\./);
  assert.match(evidence.textContent, /2 linked files were left out of the private copy/);
  assert.match(evidence.textContent, /Recorded checks/);
  assert.doesNotMatch(evidence.textContent, /Deterministic verification/);
  f.longGoal = {goal_id: "g", status: "complete", verification: {status: "passed"}};
  f.renderEvidence();
  assert.match(evidence.textContent, /Done, and the automatic checks passed\./);
});

test("a denied command says where the CLI blocks it and where it is only a request", () => {
  const f = fixture();
  const agents = [
    {agent_id: "builder", name: "Claude Code", enforcement: "cli_rule"},
    {agent_id: "reviewer", name: "Codex", enforcement: "advisory"},
  ];
  const mixed = f.goalResultNotices({
    agent_access: {grants: {d1: {decision: "deny", commands: [["npm", "publish"]]}}},
    command_denials: [{approval_digest: "d1", commands: [["npm", "publish"]], agents, note: "server note"}],
  });
  assert.equal(mixed.length, 1, "the enforcement note replaces the plain denied-command count");
  assert.equal(mixed[0],
    "You denied `npm publish`. Claude Code's CLI blocks it; for Codex this is a request, not a hard block.");
  const enforced = f.goalResultNotices({command_denials: [{commands: ["git push --force"],
    agents: [agents[0], {...agents[0], agent_id: "second", name: "Second"}]}]});
  assert.equal(enforced[0], "You denied `git push --force`. Every agent's CLI blocks it.");
  const advisory = f.goalResultNotices({command_denials: [{commands: [["rm", "-rf", "build"], ["curl", "x"], ["a"]],
    agents: [agents[1]]}]});
  assert.match(advisory[0], /^You denied `rm -rf build`, `curl x` and 1 more\. For Codex this is a request, not a hard block/);
  assert.doesNotMatch(advisory.join(" "), /fail|error/i);
  // Without the enforcement report the earlier plain note still appears.
  const plain = f.goalResultNotices({agent_access: {grants: {d1: {decision: "deny", commands: [["npm", "publish"]]}}}});
  assert.match(plain[0], /You denied 1 command request/);
});
