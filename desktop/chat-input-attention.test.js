"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const test = require("node:test");
const vm = require("node:vm");
const {chromium} = require("playwright-core");

const ui = path.join(__dirname, "../src/our_harness/ui");
const source = fs.readFileSync(path.join(ui, "app.js"), "utf8");
function section(start, end) {
  const from = source.indexOf(start), to = source.indexOf(end, from);
  assert.ok(from >= 0 && to > from, start);
  return source.slice(from, to);
}
const helpers = section("function conversationAttentionReason", "function renderTheConversationProject");
const conversations = ["seven", "eight", "nine"].map(id => ({
  id: `saved-${id}`, name: `Chat ${id}`, project: "arbitrary-project", pair: ["builder"],
}));
function fixture() {
  const context = vm.createContext({
    longGoals: [], swarmWorkRecoveries: new Map(),
    swarmChats: [{agent: "builder", conversation: conversations[0].id,
      conversations: structuredClone(conversations)}],
  });
  vm.runInContext(helpers, context);
  return {context, reason: one => context.conversationAttentionReason(one)};
}

test("only pending structured input pulses; exact chat/project, archive and terminal state are respected", () => {
  const {context: c, reason} = fixture();
  const chat = conversations[1];
  const goal = {conversation_id: chat.id, project: {id: chat.project}, status: "running"};
  c.longGoals = [goal];
  for (const status of ["running", "queued", "waiting_for_project", "paused", "failed", "complete"]) {
    goal.status = status;
    assert.equal(reason(chat), "", status);
  }
  goal.status = "waiting_for_user";
  assert.equal(reason(chat), "Needs your input");
  assert.equal(reason(conversations[0]), "", "another selected chat stays quiet");
  assert.equal(reason({...chat, project: "changed-project"}), "");
  assert.equal(reason({...chat, archived_at: "yesterday"}), "");
  goal.status = "running";
  goal.pending_interrupts = [{id: "decision", questions: [{prompt: "Choose a color"}]}];
  assert.equal(reason(chat), "Needs your input", "questions can coexist with other running agents");
  for (const status of ["complete", "cancelled", "cancelling", "failed"]) {
    goal.status = status;
    assert.equal(reason(chat), "", "old decision history must not pulse: " + status);
  }
  goal.status = "running";
  goal.pending_interrupts = [];
  assert.equal(reason(chat), "", "answer clears input state");
});

test("saved goal/recovery snapshots restore attention without an unread-message cache", () => {
  const {context: c, reason} = fixture();
  const chat = conversations[1];
  const snapshot = JSON.stringify([{conversation_id: chat.id, project: {id: chat.project},
    status: "waiting_for_user", pending_interrupts: [{id: "approval"}]}]);
  c.longGoals = JSON.parse(snapshot);
  assert.equal(reason(chat), "Needs your input");
  c.longGoals = [];
  c.swarmWorkRecoveries.set(`chat:${chat.id}`, {status: "paused_for_user", projectId: chat.project});
  assert.equal(reason(chat), "Needs your input");
  assert.equal(reason({...chat, project: "new-project"}), "");
  c.swarmWorkRecoveries.get(`chat:${chat.id}`).status = "paused_provider";
  assert.equal(reason(chat), "", "a generic failure is not a question");
});

test("background structured questions preserve selected transcript and clear on an accepted answer", () => {
  const {context: c, reason} = fixture();
  const held = c.swarmChats[0];
  const selected = held.conversation;
  held.said = [{who: "you", text: "Keep this selected history"}];
  Object.assign(c, {
    activeConversationIdFor: () => selected, swarmChatRuntimeKey: () => `chat:${selected}`,
    nextSwarmChatRevision() {}, renderTheChatThreadFor() {}, renderTheBigChat() {}, theBigOne: "builder",
  });
  vm.runInContext(section("function keepWhatWasSaidTo(", "function directLongGoalCanonicalValue"),
    // The section contains more declarations, which are inert until called.
    c);
  const chat = conversations[1];
  const deliver = said => c.keepWhatWasSaidToRuntime(`chat:${chat.id}`, said, chat.id);
  deliver([{who: "them", text: "Any news?"}]);
  assert.equal(reason(chat), "", "a question mark in ordinary prose is insufficient");
  const question = {who: "them", questions: [{id: "choice", prompt: "Which color?"}]};
  deliver([question, {who: "them", text: "Progress from another agent"}]);
  assert.equal(reason(chat), "Needs your input");
  assert.equal(held.said[0].text, "Keep this selected history");
  assert.equal(held.conversation, selected);
  deliver([question, {who: "you", text: "Blue"}]);
  assert.equal(reason(chat), "");
});

const runtime = path.join(__dirname, "build-output/win-unpacked/resources/runtime");
const manifestPath = path.join(runtime, "NEXUS_RUNTIME.json");
test("real sidebar pulses for an inactive question, survives polls/navigation, and respects reduced motion", {
  skip: !process.env.NEXUS_TEST_CHROMIUM && !fs.existsSync(manifestPath), timeout: 30000,
}, async () => {
  const manifest = fs.existsSync(manifestPath) ? JSON.parse(fs.readFileSync(manifestPath, "utf8")) : {};
  const executablePath = process.env.NEXUS_TEST_CHROMIUM
    || path.join(runtime, "playwright", manifest.playwright.chromium_executable);
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 800, height: 600}});
    await page.setContent('<aside id="theBigChatConversationList" style="width:260px;padding:16px"></aside>');
    await page.addStyleTag({content: fs.readFileSync(path.join(ui, "styles.css"), "utf8")});
    await page.addScriptTag({content: `
      const $ = id => document.getElementById(id);
      ${section("function make(tag", "function migrateGraph")}
      const swarmChats = [{agent:'builder',conversation:'saved-seven',conversations:${JSON.stringify(conversations)}}];
      let longGoals = [];
      const swarmWorkRecoveries = new Map();
      const swarmConversationSwitching = new Set();
      const theSwarmAgent = id => ({id,name:'Builder'});
      const conversationPairsFor = () => [['builder']];
      const connectedPairsFor = () => [];
      const pairKey = pair => pair.join('|');
      const anAgentFace = () => document.createElement('span');
      const swarmChatIsBusy = () => false;
      const swarmChatIsResetting = () => false;
      const swarmChatIsHydrating = () => false;
      const activateConversationFor = (_,id) => {swarmChats[0].conversation=id;renderTheConversationSidebar('builder');};
      ${section("function renderTheConversationSidebar", "function renderTheConversationProject")}
      renderTheConversationSidebar('builder');
    `});
    const waiting = page.locator('[data-chat-id="saved-eight"][data-conversation-action="pick"]');
    assert.equal(await page.locator('.needs-user-input').count(), 0);
    await page.evaluate(() => {
      longGoals = [{conversation_id:'saved-eight',project:{id:'arbitrary-project'},status:'waiting_for_user'}];
      refreshConversationAttention('builder');
    });
    assert.equal(await page.locator('.needs-user-input').count(), 1);
    assert.match(await waiting.innerText(), /Needs your input/);
    assert.equal(await page.locator('.active').getAttribute('data-chat-id'), 'saved-seven');
    const pulse = await waiting.evaluate(element => {
      const animation = element.getAnimations()[0];
      animation.pause(); animation.currentTime = 0;
      const low = getComputedStyle(element).backgroundColor;
      animation.currentTime = 1200;
      return {low, high:getComputedStyle(element).backgroundColor};
    });
    assert.notEqual(pulse.low, pulse.high, 'the color visibly changes');
    await page.evaluate(() => refreshConversationAttention('builder'));
    assert.equal(await page.locator('.chat-input-needed').count(), 1, 'polls do not duplicate the label');
    await waiting.click();
    assert.equal(await page.locator('.active.needs-user-input').count(), 1, 'opening is not answering');
    const evidence = process.env.NEXUS_ATTENTION_EVIDENCE || fs.mkdtempSync(path.join(os.tmpdir(), 'nexus-attention-'));
    fs.mkdirSync(evidence, {recursive:true});
    await page.screenshot({path:path.join(evidence,'sidebar.png')});
    console.log('Attention screenshot: ' + path.join(evidence,'sidebar.png'));
    await page.emulateMedia({reducedMotion:'reduce'});
    assert.equal(await waiting.evaluate(element => getComputedStyle(element).animationName), 'none');
    assert.match(await waiting.innerText(), /Needs your input/);
    await page.evaluate(() => {longGoals[0].status='running';refreshConversationAttention('builder');});
    assert.equal(await page.locator('.needs-user-input').count(), 0);
    assert.equal(await page.locator('.chat-input-needed').count(), 0);
  } finally { await browser.close(); }
});
