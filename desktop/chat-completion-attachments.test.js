"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const {chromium} = require("playwright-core");

const ui = path.join(__dirname, "../src/our_harness/ui");
const source = fs.readFileSync(path.join(ui, "app.js"), "utf8");
function section(start, end, from = 0) {
  const begin = source.indexOf(start, from), finish = source.indexOf(end, begin);
  assert.ok(begin >= 0 && finish > begin, start);
  return source.slice(begin, finish);
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}
function attachmentFixture() {
  const errors = [], renders = [];
  const context = vm.createContext({
    agent: "shared", chat: "red", swarmChatAttachments: new Map(), swarmChatAttachmentLoads: new Map(),
    swarmChatKey: () => `shared:${context.chat}`,
    setWhatCanBePressedInSwarm() {}, showError: text => errors.push(text),
  });
  vm.runInContext(section("async function addChatAttachments", "function attachmentChip"), context);
  context.readChatAttachment = file => file.promise;
  context.renderChatAttachments = agent => renders.push([agent, context.chat]);
  return {context, errors, renders, add: files => context.addChatAttachments("shared", files)};
}
function file(name, data = name, size = data.length) { return {name, type: "text/plain", size, data}; }

test("only supported Nexus completion records are promoted, without altering stored turns", () => {
  const context = vm.createContext({});
  vm.runInContext(section("function isNexusChatTurn", "function aChatTurnFace")
    + section("function normalizedLongHorizonCorrelation", "const PARTICIPANT_OUTCOME_STATUSES"), context);
  const record = {speaker_id:"nexus",phase:"long_horizon_status",text:"Saved event",
    correlation:{schema_version:1,kind:"long_horizon_status",goal_id:"exact-goal",goal_status:"complete"}};
  for (const changes of [{speaker_id:"builder"},{phase:"agent_discussion"},{structured_state_unavailable:true},
    {correlation:{...record.correlation,goal_status:"running"}}, {correlation:{...record.correlation,schema_version:2}},
    {questions:[{text:"Choose next action"}]}, {attachments:[{name:"evidence.txt"}]}]) {
    assert.equal(context.chatGoalCompletion({...record,...changes}), null);
  }
  const original = [record,{...record,text:"Latest record"}];
  const before = JSON.stringify(original);
  const rendered = context.uniqueChatCompletionTurns(original);
  assert.equal(rendered.length,1);
  assert.equal(rendered[0].text,"Latest record");
  assert.equal(JSON.stringify(original),before);
});

test("overlapping reads, removal and chat switches preserve exact attachment ownership", async () => {
  const f = attachmentFixture();
  const original = file("existing.txt");
  f.context.swarmChatAttachments.set("shared:red", [original]);
  const slow = deferred(), fast = deferred();
  const readingSlow = f.add([slow]), readingFast = f.add([fast]);
  assert.equal(f.context.swarmChatAttachmentLoads.get("shared:red"), 2);
  f.context.removeChatAttachment("shared", 0);
  f.context.chat = "blue";
  f.context.swarmChatAttachments.set("shared:blue", [file("other-chat.txt")]);
  fast.resolve(file("fast.txt")); await readingFast;
  slow.resolve(file("slow.txt")); await readingSlow;
  assert.deepEqual(Array.from(f.context.swarmChatAttachments.get("shared:red"), one => one.name), ["fast.txt", "slow.txt"]);
  assert.equal(f.context.swarmChatAttachments.get("shared:blue")[0].name, "other-chat.txt");
  assert.equal(f.context.swarmChatAttachmentLoads.size, 0);
  assert.deepEqual(f.errors, []);
});

test("identical additions deduplicate while different bytes and attachment limits remain enforced", async () => {
  const f = attachmentFixture();
  const add = async values => {
    const pending = values.map(() => deferred());
    const reading = f.add(pending);
    pending.forEach((one, i) => one.resolve(values[i]));
    await reading;
  };
  await add([file("same.txt", "first")]);
  await add([file("same.txt", "first"), file("same.txt", "different")]);
  assert.equal(f.context.swarmChatAttachments.get("shared:red").length, 2);
  await add([file("3"), file("4"), file("5"), file("6")]);
  await add([file("7")]);
  assert.match(f.errors.pop(), /at most 6/);
  assert.equal(f.context.swarmChatAttachments.get("shared:red").length, 6);
  f.context.swarmChatAttachments.clear();
  await add([file("a", "a", 4000000), file("b", "b", 4000000), file("c", "c", 1)]);
  assert.match(f.errors.pop(), /8 MB/);
  assert.equal(f.context.swarmChatAttachments.size, 0);
  const bad = deferred(); const reading = f.add([bad]); bad.reject(new Error("File could not be read")); await reading;
  assert.match(f.errors.pop(), /could not be read/);
  assert.equal(f.context.swarmChatAttachmentLoads.size, 0);
});

const runtime = path.join(__dirname, "build-output/win-unpacked/resources/runtime");
const manifestPath = path.join(runtime, "NEXUS_RUNTIME.json");
test("real compact/full renderers show one Nexus completion bubble and share picker/paste attachment chips", {
  skip: !fs.existsSync(manifestPath) && !process.env.NEXUS_TEST_CHROMIUM, timeout: 45000,
}, async () => {
  const manifest = fs.existsSync(manifestPath) ? JSON.parse(fs.readFileSync(manifestPath, "utf8")) : {};
  const executablePath = process.env.NEXUS_TEST_CHROMIUM || path.join(runtime, "playwright", manifest.playwright.chromium_executable);
  const browser = await chromium.launch({executablePath, headless: true});
  const output = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-completion-paste-"));
  try {
    const page = await browser.newPage({viewport: {width: 1060, height: 820}});
    await page.setContent(fs.readFileSync(path.join(ui, "index.html"), "utf8")
      .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, "").replace(/<link\b[^>]*>/gi, ""));
    await page.addStyleTag({content: fs.readFileSync(path.join(ui, "styles.css"), "utf8")});
    const icon = `data:image/x-icon;base64,${fs.readFileSync(path.join(__dirname, "nexus-harness.ico")).toString("base64")}`;
    const renderStart = source.indexOf("function renderTheBigChat");
    await page.addScriptTag({content: `
      const $ = id => document.getElementById(id);
      const nexusAppIconDataUrl = ${JSON.stringify(icon)};
      let theBigOne = 'shared'; const agent = {id:'shared',name:'Builder'};
      const held = {agent:'shared',conversation:'red'}; const swarmChats = [held];
      const swarmChatAttachments = new Map(), swarmChatAttachmentLoads = new Map();
      const swarmChatKey = () => 'shared:' + held.conversation;
      const swarmChatRuntimeKey = () => 'chat:' + held.conversation;
      function setWhatCanBePressedInSwarm() {} function countWhatIsTypedTo() {}
      function showError(error) { window.lastError = error; }
      ${section("function make(tag", "function migrateGraph")}
      ${section("function isNexusChatTurn", "function agentForChatTurn")}
      ${section("function appendChatText", "const PARTICIPANT_OUTCOME_STATUSES")}
      ${section("function chatTurnSpeaker", "let userQuestionRenderId")}
      ${section("function putTheChatTurnsIn", "function renderTheChatThreadFor")}
      ${section("function readChatAttachment", "function oneSwarmChatCard")}
      ${section("function clearSwarmActivityAttachments", "function normalizedUserQuestions")}
      const compact = make('div', 'swarm-chat-card'); compact.id='fixtureCompact';
      const box = make('textarea', 'swarm-chat-box'); compact.append(box);
      compact.append(make('div', 'chat-attachments')); document.body.append(compact);
      function theChatCardFor() { return compact; }
      function theSwarmAgent() { return agent; } function agentForChatTurn() { return agent; }
      function styleForAgent() {} function normalizedParticipantOutcome() { return null; }
      function appendInlineUserQuestions() {} function prettyTime(value) { return value + ' ms'; }
      function anAgentFace() { return make('span'); } function aFaceFor() { return make('span'); }
      async function openChatGoalDetails(value) { window.openedGoal = value.goal_id; }
      ${source.split('\n').find(line => line.includes('box.addEventListener("paste", event => pasteChatAttachments'))}
      ${source.split('\n').find(line => line.includes('$("theBigChatBox").addEventListener("paste"'))}
      ${section('  $("theBigChatFiles").addEventListener("change"', '  $("theBigChatProject").addEventListener')}
      function renderFixtureFull(raw) {
        const list = $('theBigChatSaid'); const chatTurnsWhileWorking = () => raw, keptTranscriptFor = () => raw;
        ${section("  const turns = [];", "  // What this agent said", renderStart)}
        list.replaceChildren();
        ${section("    for (const one of turns) {", "    list.scrollTop = list.scrollHeight;", renderStart)}
      }
    `});
    await page.evaluate(() => {
      for (let node = $("theBigChat"); node; node = node.parentElement) node.hidden = false;
      const complete = (id, text) => ({who:'them',speaker_id:'nexus',phase:'long_horizon_status',text,
        correlation:{schema_version:1,kind:'long_horizon_status',goal_id:id,goal_status:'complete'}});
      window.savedTurns = [complete('goal-one','Earlier saved record'), complete('goal-one','Latest saved completion record'),
        complete('goal-two','Legacy verification was not configured.')];
      renderFixtureFull(JSON.parse(JSON.stringify(window.savedTurns)));
      const list = make('ol'); list.id='compactTranscript'; document.getElementById('fixtureCompact').append(list);
      putTheChatTurnsIn(list, agent, window.savedTurns, false);
    });
    for (const selector of ['#theBigChatSaid', '#compactTranscript']) {
      assert.equal(await page.locator(`${selector} .chat-goal-completion`).count(), 2);
      assert.equal(await page.locator(`${selector} .chat-goal-status-row`).count(), 0);
      assert.equal(await page.locator(`${selector} .chat-goal-completion-title`).first().innerText(), 'Task completed');
      assert.equal(await page.locator(`${selector} img[alt="Nexus Harness"]`).count(), 2);
      assert.equal(await page.locator(`${selector} .chat-goal-completion-record`).first().getAttribute('open'), null);
      assert.doesNotMatch(await page.locator(selector).innerText(), /verified complete|Earlier saved/);
    }
    await page.locator('#theBigChatSaid .chat-goal-completion').first().getByRole('button', {name:'Open goal details'}).click();
    assert.equal(await page.evaluate(() => window.openedGoal), 'goal-one');
    await page.locator('#theBigChatSaid .chat-goal-completion-record').first().locator('summary').click();
    assert.match(await page.locator('#theBigChatSaid').innerText(), /Latest saved completion record/);
    await page.locator('#theBigChatSaid .chat-goal-completion-record').first().locator('summary').click();
    await page.locator('#theBigChatFiles').setInputFiles({name:'picked.txt',mimeType:'text/plain',buffer:Buffer.from('picked file')});
    await page.waitForFunction(() => swarmChatAttachmentLoads.size === 0);
    await page.evaluate(() => {
      const transfer = new DataTransfer();
      transfer.items.add(new File(['copied file'], 'copied.txt', {type:'text/plain'}));
      $('theBigChatBox').dispatchEvent(new ClipboardEvent('paste', {clipboardData:transfer,bubbles:true,cancelable:true}));
    });
    await page.waitForFunction(() => swarmChatAttachmentLoads.size === 0);
    await page.evaluate(() => {
      const bytes = Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aCdwAAAAASUVORK5CYII='), x => x.charCodeAt(0));
      const transfer = new DataTransfer(); transfer.items.add(new File([bytes], 'clipboard.png', {type:'image/png'}));
      document.querySelector('#fixtureCompact textarea').dispatchEvent(new ClipboardEvent('paste', {clipboardData:transfer,bubbles:true,cancelable:true}));
    });
    await page.waitForFunction(() => swarmChatAttachmentLoads.size === 0);
    for (const selector of ['#theBigChatAttachments', '#fixtureCompact .chat-attachments']) {
      assert.deepEqual(await page.locator(`${selector} .chat-attachment-name`).allTextContents(), ['picked.txt','copied.txt','clipboard.png']);
      assert.equal(await page.locator(`${selector} img`).count(), 1);
    }
    await page.screenshot({path:path.join(output,'completion-and-attachments.png'),fullPage:false});
    await page.evaluate(() => { $('theBigChat').hidden = true; });
    await page.locator('#fixtureCompact').screenshot({path:path.join(output,'compact-completion-and-attachments.png')});
    await page.evaluate(() => { $('theBigChat').hidden = false; });
    const busyPaste = await page.evaluate(() => {
      $('theBigChatAttach').disabled = true;
      const transfer = new DataTransfer(); transfer.items.add(new File(['busy'], 'busy.txt', {type:'text/plain'}));
      const event = new ClipboardEvent('paste', {clipboardData:transfer,bubbles:true,cancelable:true});
      $('theBigChatBox').dispatchEvent(event);
      $('theBigChatAttach').disabled = false;
      return {prevented:event.defaultPrevented,names:swarmChatAttachments.get(swarmChatKey()).map(one=>one.name),error:window.lastError};
    });
    assert.equal(busyPaste.prevented,true);
    assert.equal(busyPaste.names.includes('busy.txt'),false);
    assert.match(busyPaste.error,/Attach button/);
    const oversize = await page.evaluate(async () => {
      await addChatAttachments('shared',[new File([new Uint8Array(4000001)],'too-large.bin')]);
      return {error:window.lastError,count:swarmChatAttachments.get(swarmChatKey()).length,loading:swarmChatAttachmentLoads.size};
    });
    assert.match(oversize.error,/4 MB/);
    assert.equal(oversize.count,3);
    assert.equal(oversize.loading,0);
    const textPaste = await page.evaluate(() => {
      const transfer = new DataTransfer(); transfer.setData('text/plain', 'normal text');
      const event = new ClipboardEvent('paste', {clipboardData:transfer,bubbles:true,cancelable:true});
      $('theBigChatBox').dispatchEvent(event); return event.defaultPrevented;
    });
    assert.equal(textPaste, false);
    await page.locator('#theBigChatAttachments').getByRole('button', {name:'Remove copied.txt'}).click();
    assert.deepEqual(await page.locator('#fixtureCompact .chat-attachment-name').allTextContents(), ['picked.txt','clipboard.png']);
    const ownership = await page.evaluate(() => {
      const staleRemove = document.querySelector('#theBigChatAttachments button');
      held.conversation = 'blue'; swarmChatAttachments.set(swarmChatKey(), [{name:'blue.txt',type:'text/plain',data:'blue',size:4}]);
      renderChatAttachments('shared'); staleRemove.click();
      const blue = swarmChatAttachments.get('shared:blue').map(one => one.name);
      const red = swarmChatAttachments.get('shared:red').map(one => one.name);
      clearSwarmActivityAttachments({stateKey:'shared:red',chatKey:'chat:red'});
      return {blue,red,redSent:!swarmChatAttachments.has('shared:red'),blueStillPresent:swarmChatAttachments.has('shared:blue')};
    });
    assert.deepEqual(ownership,{blue:['blue.txt'],red:['clipboard.png'],redSent:true,blueStillPresent:true});
    console.log('Completion/paste evidence:', output);
  } finally { await browser.close(); }
});
