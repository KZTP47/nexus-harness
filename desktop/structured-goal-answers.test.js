"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../src/our_harness/ui/app.js"), "utf8");
const section = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));

function element(tag, className = "", text = "") {
  return {tag, className, textContent: text, value: "", dataset: {}, style: {removeProperty() {}, setProperty() {}}, classList: {toggle() {}}, children: [], listeners: {},
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    setAttribute(name, value) { this[name] = value; },
    addEventListener(name, callback) { this.listeners[name] = callback; },
    emit(name, event = {}) { return this.listeners[name]?.(event); },
    all() { return this.children.flatMap(child => [child, ...(child.all?.() || [])]); },
    querySelectorAll(selector) {
      const name = selector.match(/name="([^"]+)"/)?.[1];
      return this.all().filter(one => one.tag === "input" && one.checked
        && (name ? one.name === name : one.dataset.questionChoice === "true"));
    },
    querySelector(selector) {
      if (selector.startsWith('.')) return this.all().find(one => one.className.split(' ').includes(selector.slice(1))) || null;
      if (selector === 'details') return this.all().find(one => one.tag === 'details') || null;
      const name = selector.match(/data-question-name="([^"]+)"/)?.[1];
      return this.all().find(one => one.dataset.questionName === name);
    },
  };
}

function fixture(questions = [{id: "folder", prompt: "Use misspelled PLOQGZ?", options: [], allow_other: true}]) {
  const panel = element("section");
  const nodes = {missionInboxCount: element("span"), missionInbox: element("section")};
  const f = {panel, calls: [], failures: 0, goal: {goal_id: "goal-exact", revision: 9, status: "waiting_for_user",
    pending_interrupts: [{id: "interrupt-exact", task_id: "task-1", created_ms: 1, questions}]}};
  const context = vm.createContext({
    make: element, document: {createElement: element}, CSS: {escape: value => value},
    crypto: require("node:crypto").webcrypto, longHorizonStateWords: value => value,
    chatLongGoalContext: () => ({goal: f.goal, problem: ""}), swarmChatKey: () => "saved-chat",
    chatGoalBinding: () => ({chat_id: "saved-chat"}),
    appendGoalAccessControls() {},
    openChatGoalDetails() {}, refreshChatGoalAfterAction(agent, goal) { f.accepted = goal; }, setWhatCanBePressedInSwarm() {},
    rememberChatGoalSnapshot(goal) { f.accepted = goal; return goal; },
    beginGoalSnapshotRead: () => 1,
    refreshLongGoals() {}, showError: value => { f.error = value; },
    $: id => nodes[id], immutable: false, providerSetupChanged: false,
    request: async (url, options) => {
      f.calls.push({url, body: options ? JSON.parse(options.body) : null});
      if (f.failures-- > 0) throw new Error("Lost response; retry the same answer");
      if (!options) {
        if (f.readHook) await f.readHook();
        return {goal: f.readGoal || f.goal};
      }
      return {goal: f.responseGoal || f.goal};
    },
  });
  vm.runInContext(section("const chatGoalRequests =", "function chatGoalParticipants"), context);
  vm.runInContext(section("function goalSnapshotIdentity", "function rememberChatGoalSnapshot"), context);
  vm.runInContext(section("function normalizedUserQuestions", "function userQuestionFields"), context);
  // End at the next top-level function, not at nested event callbacks.
  const fieldsStart = source.indexOf("function userQuestionFields");
  const fieldsEnd = source.indexOf("\nfunction ", fieldsStart + 1);
  vm.runInContext(source.slice(fieldsStart, fieldsEnd), context);
  vm.runInContext(section("function fillChatGoalPanel", "function syncChatGoalControls"), context);
  context.fillChatGoalPanel(panel, "agent", {goal: f.goal, problem: ""});
  f.context = context;
  f.submit = () => panel.all().find(one => one.className.split(" ").includes("chat-goal-answer")).emit("click");
  f.textarea = () => panel.all().find(one => one.tag === "textarea");
  f.audience = () => panel.all().find(one => one.tag === "select");
  f.reconsider = () => panel.all().find(one => one.textContent === "Reconsider using saved answers");
  f.render = () => context.fillChatGoalPanel(panel, "agent", {goal: f.goal, problem: ""});
  f.mission = () => {
    context.longGoal = f.goal;
    vm.runInContext(section("  const pending = longGoal?.pending_interrupts || [];", "  const evidence = $(\"missionEvidence\");"), context);
    return nodes.missionInbox.children.find(one => one.tag === "form");
  };
  return f;
}

test("actual chat answer form separates exact typed correction from the mistaken prompt and selects audience", async () => {
  const f = fixture();
  const exact = `  ${path.join(os.tmpdir(), "PLOQQIZ", "new folder", "file.html")}\nhttps://example.test/a%20b?q=A+B#Exact  `;
  f.textarea().value = exact;
  f.textarea().emit("input");
  f.audience().value = "requesting_agent";
  f.audience().emit("change");
  await f.submit();
  assert.deepEqual(f.calls[0].body.answers["interrupt-exact"], {schema_version: 1, audience: "requesting_agent",
    questions: [{question_id: "folder", selected_options: [], text: exact}]});
  assert.ok(!JSON.stringify(f.calls[0].body.answers).includes("PLOQGZ"));
});

test("multiple selected labels and custom text both survive the actual chat form", async () => {
  const f = fixture([{id: "choices", prompt: "Choose references", multiple: true, allow_other: true,
    options: [{label: "Folder A"}, {label: "Folder B"}]}]);
  for (const one of f.panel.all().filter(one => one.tag === "input" && one.type === "checkbox")) {
    one.checked = true;
    one.emit("change");
  }
  const custom = f.panel.all().find(one => one.tag === "input" && !one.type);
  custom.value = path.join(os.tmpdir(), "PLOQQIZ", "Exact");
  custom.emit("input");
  await f.submit();
  assert.deepEqual(f.calls[0].body.answers["interrupt-exact"], {schema_version: 1, audience: "team",
    questions: [{question_id: "choices", selected_options: ["Folder A", "Folder B"], text: custom.value}]});
});

test("saved choices-only team questions accept a typed correction in chat and mission control", async () => {
  const questions = [{id: "destination", prompt: "Which destination?", allow_other: false,
    options: [{label: "First folder"}, {label: "Second folder"}]}];
  for (const surface of ["chat", "mission"]) {
    const f = fixture(questions);
    const form = surface === "mission" ? f.mission() : f.panel;
    const input = form.all().find(one => surface === "mission" ? one.tag === "textarea"
      : one.tag === "input" && !one.type);
    assert.ok(input, surface);
    input.value = `Use ${path.join(os.tmpdir(), "different project", "game")}`;
    input.emit("input");
    f.render();
    assert.equal(input.value.startsWith("Use "), true);
    if (surface === "mission") await form.emit("submit", {preventDefault() {}});
    else await f.submit();
    assert.deepEqual(f.calls[0].body.answers["interrupt-exact"].questions,
      [{question_id: "destination", selected_options: [], text: input.value}]);
  }
});

test("engine risk approvals retain explicit choices on both decision surfaces", () => {
  const f = fixture([{id: "approval", prompt: "Continue?", allow_other: false,
    options: [{label: "Continue with checks"}, {label: "Stop this task"}]}]);
  f.goal.pending_interrupts[0].purpose = "risk_review";
  f.render();
  for (const form of [f.panel, f.mission()]) {
    assert.equal(form.all().some(one => one.tag === "textarea" || one.tag === "input" && !one.type), false);
  }
});

test("lost response retry keeps the same logical request while edited answers get another identity", async () => {
  const f = fixture();
  f.textarea().value = "PLOQQIZ";
  f.textarea().emit("input");
  f.failures = 1;
  await f.submit();
  await f.submit();
  assert.equal(f.calls[0].body.request_id, f.calls[1].body.request_id);
  f.textarea().value = "PLOQQIZ/subfolder";
  f.textarea().emit("input");
  await f.submit();
  assert.notEqual(f.calls[1].body.request_id, f.calls[2].body.request_id);
});

test("advanced goal form preserves raw values and refuses stale goal identity", async () => {
  const f = fixture();
  const form = f.mission();
  const text = path.join(os.tmpdir(), "PLOQQIZ", "new folder", "index.html");
  form.all().find(one => one.tag === "textarea").value = text;
  await form.emit("submit", {preventDefault() {}});
  assert.deepEqual(f.calls[0].body.answers["interrupt-exact"], {schema_version: 1, audience: "team",
    questions: [{question_id: "folder", selected_options: [], text}]});
  f.context.longGoal = {...f.goal, goal_id: "different-goal"};
  await form.emit("submit", {preventDefault() {}});
  assert.equal(f.calls.length, 1);
  assert.match(f.error, /goal or its questions changed/);
});

for (const surface of ["chat", "advanced"]) {
  function recoveryFixture() {
    const f = fixture();
    f.goal.decision_reconsideration = {available: true, pending_ids: ["interrupt-exact"]};
    if (surface === "chat") {
      f.render();
      f.button = f.reconsider();
    } else {
      f.mission();
      f.button = f.context.$("missionInbox").all().find(one => one.textContent === "Reconsider using saved answers");
    }
    return f;
  }

  test(`${surface}: the visible reconsider button sends exact pending identity without inventing an answer`, async () => {
    const f = recoveryFixture();
    f.responseGoal = {...f.goal, status: "queued", revision: 10, pending_interrupts: []};
    await f.button.emit("click");
    assert.equal(f.calls[0].url, "/api/long-horizon/goal?id=goal-exact");
    const post = f.calls.find(one => one.body);
    assert.equal(post.url, "/api/long-horizon/reconsider");
    assert.deepEqual(post.body, {goal_id: "goal-exact", expected_revision: 9, pending_ids: ["interrupt-exact"],
      ...(surface === "chat" ? {chat_id: "saved-chat"} : {})});
    assert.equal(f.accepted, f.responseGoal);
    assert.equal(f.accepted.pending_interrupts.length, 0);
  });

  test(`${surface}: reconsider preserves the card and draft on failure, and refuses a changed question`, async () => {
    const f = recoveryFixture();
    f.failures = 1;
    const textarea = surface === "chat" ? f.textarea() : f.context.$("missionInbox").all().find(one => one.tag === "textarea");
    textarea.value = "Keep my unsent PLOQQIZ correction";
    await f.button.emit("click");
    assert.equal(f.button.disabled, false);
    assert.equal(f.accepted, undefined);
    assert.equal(textarea.value, "Keep my unsent PLOQQIZ correction");
    f.goal.pending_interrupts = [{...f.goal.pending_interrupts[0], id: "different-interrupt"}];
    await f.button.emit("click");
    assert.equal(f.calls.length, 1);
  });

  test(`${surface}: scheduler release refreshes the revision without replacing the displayed decision`, async () => {
    const f = recoveryFixture();
    f.readGoal = {...f.goal, revision: 10, worker: {pid: 0, worker_id: ""}};
    await f.button.emit("click");
    assert.equal(f.calls.find(one => one.body).body.expected_revision, 10);
  });

  test(`${surface}: a preflight cannot substitute a changed question, authority, saved answer, or selection`, async () => {
    for (const changed of ["question", "authority", "saved answer", "selection"]) {
      const f = recoveryFixture();
      f.readGoal = {...f.goal,revision:10};
      if (changed === "question") f.readGoal.pending_interrupts = [{...f.goal.pending_interrupts[0],questions:[]}];
      if (changed === "authority") f.readGoal.project_authority_id = "different-authority";
      if (changed === "saved answer") f.readGoal.interrupts = [{id:"new-answer",state:"resolved",answer:"new authority?"}];
      if (changed === "selection") f.readHook = () => {
        f.goal = {...f.goal,goal_id:"different-goal"};
        f.context.longGoal = f.goal;
      };
      await f.button.emit("click");
      assert.equal(f.calls.some(one => one.body), false, changed);
    }
  });
}

test("reconsider is absent without server eligibility and appears when eligibility changes during polling", () => {
  const f = fixture();
  assert.equal(f.reconsider(), undefined);
  f.goal.decision_reconsideration = {available: false, pending_ids: ["interrupt-exact"]};
  f.render();
  assert.equal(f.reconsider(), undefined);
  f.goal.decision_reconsideration.available = true;
  f.render();
  assert.ok(f.reconsider());
  f.goal.decision_reconsideration.pending_ids = ["other-interrupt"];
  f.render();
  assert.equal(f.reconsider(), undefined);
});
