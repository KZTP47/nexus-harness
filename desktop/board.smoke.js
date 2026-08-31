"use strict";

// Does the agent board work in the app somebody installs?
//
// The board's Add an agent and Add a project both ask for one line of text.
// That is the exact thing that was broken for months everywhere else: the
// panel asked with window.prompt, which Electron does not have, so pressing
// the button opened no box, printed no error, and changed nothing - while
// every browser check passed, because a browser has prompt and the app does
// not.
//
// So this one runs in the real app. It opens the board, presses Add an agent,
// types in the box that opens, presses OK, and looks at whether an agent
// really landed on the board. Then it takes it off again and puts the board
// back the way it found it.
//
// It needs a screen, so a build server cannot run it. Anybody about to send
// this to somebody else can, and should:
//
//     npm run smoke:board

const path = require("node:path");
const fs = require("node:fs");
const { _electron: electron } = require("playwright-core");

const TIMEOUT_MS = 120000;
const OUTPUT = path.join(__dirname, "build-output");
const CALLED = "Added by a smoke check";
const GIVEN_APP = process.argv[2] || "";

function theBuiltApp() {
  if (GIVEN_APP) {
    const given = path.resolve(GIVEN_APP);
    if (!fs.existsSync(given)) throw new Error(`The app does not exist: ${given}`);
    return given;
  }
  for (const name of fs.readdirSync(OUTPUT)) {
    const folder = path.join(OUTPUT, name);
    if (!fs.statSync(folder).isDirectory() || !name.includes("unpacked")) continue;
    const found = fs.readdirSync(folder)
      .find((one) => one.endsWith(".exe") && !one.startsWith("Uninstall"));
    if (found) return path.join(folder, found);
    const app = fs.readdirSync(folder).find((one) => one.endsWith(".app"));
    if (app) return path.join(folder, app);
  }
  throw new Error(`No built app in ${OUTPUT}. Build it first: npm run build`);
}

async function reachThePanel(page) {
  const first = await Promise.race([
    page.waitForFunction(() => location.protocol === "http:", null, { timeout: 90000 })
      .then(() => "panel"),
    page.waitForSelector("#repair", { state: "visible", timeout: 90000 })
      .then(() => "repair"),
  ]);
  if (first === "repair") {
    await page.click("#repair");
    await page.waitForFunction(
      () => location.protocol === "http:", null, { timeout: 90000 }
    );
    console.log("pass  the packaged app repaired a newer-project mismatch");
  }
}

async function main() {
  const exe = theBuiltApp();
  console.log(`Starting the built app at ${exe}\n`);
  const app = await electron.launch({ executablePath: exe, args: [], timeout: TIMEOUT_MS });
  const page = await app.firstWindow({ timeout: TIMEOUT_MS });
  let putBack = null;
  try {
    await reachThePanel(page);
    await page.click('[data-view="swarm"]', { timeout: 30000 });
    await page.waitForFunction(
      () => !document.getElementById("swarmView").hidden
        && document.getElementById("swarmSaid").textContent.length > 0,
      null, { timeout: 30000 }
    );
    console.log("pass  the board opens");

    // This must be the app window's full screen, not only the browser API. The
    // latter worked in a browser while the installed app's button did nothing.
    const nativeWindow = await app.browserWindow(page);
    await page.click("#swarmFullScreen", { timeout: 20000 });
    await page.waitForFunction(
      () => document.getElementById("swarmStage").classList.contains("is-fullscreen")
        && document.getElementById("swarmFullScreen").textContent === "Exit full screen",
      null, { timeout: 20000 }
    );
    if (!await nativeWindow.evaluate((window) => window.isFullScreen())) {
      throw new Error("the board changed shape but the Electron window did not enter full screen");
    }
    console.log("pass  the board fills the real Electron window");
    await page.click("#swarmFullScreen", { timeout: 20000 });
    await page.waitForFunction(
      () => !document.getElementById("swarmStage").classList.contains("is-fullscreen"),
      null, { timeout: 20000 }
    );

    await page.click('[data-view="pipelines"]', { timeout: 20000 });
    await page.waitForSelector("#pipelineNodes .pipeline-node", { timeout: 30000 });
    await page.click("#pipelineFullScreen", { timeout: 20000 });
    await page.waitForFunction(
      () => document.getElementById("pipelineStage").classList.contains("is-fullscreen")
        && document.getElementById("pipelineLibraryControls").parentElement.id === "pipelineFocusSide"
        && document.getElementById("pipelinePalette").parentElement.id === "pipelineFocusSide",
      null, { timeout: 20000 }
    );
    if (!await nativeWindow.evaluate((window) => window.isFullScreen())) {
      throw new Error("the pipeline changed shape but the Electron window did not enter full screen");
    }
    console.log("pass  the pipeline, automation controls, and flow steps fill the real Electron window");
    await page.click("#pipelineZoomOut", { timeout: 20000 });
    await page.waitForFunction(
      () => document.getElementById("pipelineZoomValue").textContent !== "100%",
      null, { timeout: 20000 }
    );
    console.log("pass  the full-screen pipeline can zoom");
    await page.click("#pipelineNew", { timeout: 20000 });
    await page.waitForSelector("#askDialog[open]", { timeout: 15000 });
    const smokeAutomation = `Blank smoke automation ${Date.now()}`;
    await page.fill("#askDialogInput", smokeAutomation);
    await page.click("#askDialogOk");
    await page.waitForFunction(
      (wanted) => document.getElementById("pipelineName").value === wanted
        && document.querySelectorAll("#pipelineNodes .pipeline-node").length === 0
        && [...document.querySelectorAll("#pipelineList .pipeline-saved-one")]
          .some((button) => button.textContent === wanted && button.classList.contains("chosen")),
      smokeAutomation, { timeout: 20000 }
    );
    console.log("pass  a named new automation is saved, listed, selected, and blank");
    page.once("dialog", (dialog) => dialog.accept());
    await page.click("#pipelineDelete", { timeout: 20000 });
    await page.waitForFunction(
      (wanted) => ![...document.querySelectorAll("#pipelineList .pipeline-saved-one")]
        .some((button) => button.textContent === wanted),
      smokeAutomation, { timeout: 20000 }
    );
    await page.click("#pipelineFullScreen", { timeout: 20000 });
    await page.waitForFunction(
      () => !document.getElementById("pipelineStage").classList.contains("is-fullscreen")
        && document.getElementById("pipelinePalette").parentElement.classList.contains("pipeline-side"),
      null, { timeout: 20000 }
    );
    await page.click('[data-view="swarm"]', { timeout: 20000 });

    // What this machine's board holds, so it can be put back whatever happens.
    putBack = await page.evaluate(async () => (await request("/api/swarm")).board);

    await page.click("#swarmAddProject", { timeout: 20000 });
    await page.waitForSelector("#askDialog[open]", { timeout: 15000 });
    if (!await page.locator("#askDialogBrowse").isVisible()) {
      throw new Error("the packaged app did not offer its native folder picker");
    }
    console.log("pass  Add another project folder offers Browse folder in the packaged app");
    await page.click("#askDialogCancel", { timeout: 15000 });

    await page.click("#swarmAddAgent", { timeout: 20000 });
    await page.waitForSelector("#askDialog[open]", { timeout: 15000 });
    console.log("pass  pressing Add an agent opens a box to type in");

    await page.fill("#askDialogInput", CALLED);
    await page.click("#askDialogOk");
    await page.waitForFunction(
      (wanted) => [...document.querySelectorAll(".swarm-box.agent .swarm-box-name")]
        .some((one) => one.textContent === wanted),
      CALLED, { timeout: 30000 }
    );
    console.log("pass  what was typed really became an agent on the board");

    // And its settings opened on the right, which is what makes the board
    // worth pressing: the agent just added is the one you can change.
    await page.waitForFunction(
      (wanted) => document.getElementById("swarmPanelTitle").textContent === wanted
        && !document.getElementById("swarmAgentPanel").hidden,
      CALLED, { timeout: 20000 }
    );
    console.log("pass  the agent just added is the one whose settings are open");

    // The gear on the box, which is how the drawing asks for it to be reached.
    // Nothing is picked first, so the gear really is what opened it - and it is
    // this agent's own gear, not whichever box happens to sort first. Taking the
    // first one renamed and read back the wrong agent everywhere else this went
    // wrong, and it reads exactly like the bug a check is here to catch.
    const which = await page.evaluate(
      (wanted) => [...document.querySelectorAll(".swarm-box.agent")]
        .find((one) => one.querySelector(".swarm-box-name").textContent === wanted)
        .dataset.id,
      CALLED
    );
    await page.evaluate(() => { swarmPicked = null; renderSwarmPanel(); });
    await page.click(
      `.swarm-box[data-id="${which}"] .swarm-icon-button[data-does="settings"]`,
      { timeout: 20000 });
    await page.waitForFunction(
      (wanted) => document.getElementById("swarmPanelTitle").textContent === wanted,
      CALLED, { timeout: 20000 }
    );
    console.log("pass  the gear on the box opens its settings");

    await page.click("#swarmFullScreen", { timeout: 20000 });
    await page.waitForFunction(
      () => document.getElementById("swarmStage").classList.contains("is-fullscreen")
        && document.getElementById("swarmPanel").parentElement.id === "swarmStage"
        && !document.getElementById("swarmPanel").hidden
        && getComputedStyle(document.getElementById("swarmPanelClose")).display !== "none",
      null, { timeout: 20000 }
    );
    await page.click("#swarmPanelClose", { timeout: 20000 });
    await page.waitForFunction(
      () => document.getElementById("swarmPanel").hidden
        && document.getElementById("swarmStage").classList.contains("is-fullscreen"),
      null, { timeout: 20000 }
    );
    if (!await nativeWindow.evaluate((window) => window.isFullScreen())) {
      throw new Error("closing the right panel also closed the Electron window's full screen");
    }
    await page.click(
      `.swarm-box[data-id="${which}"] .swarm-icon-button[data-does="settings"]`,
      { timeout: 20000 });
    await page.waitForFunction(
      () => !document.getElementById("swarmPanel").hidden
        && document.getElementById("swarmPanel").parentElement.id === "swarmStage",
      null, { timeout: 20000 }
    );
    console.log("pass  the right settings panel opens, closes, and reopens inside board full screen");
    await page.click("#swarmFullScreen", { timeout: 20000 });
    await page.waitForFunction(
      () => !document.getElementById("swarmStage").classList.contains("is-fullscreen")
        && document.getElementById("swarmPanel").parentElement.id === "swarmView"
        && !document.getElementById("swarmPanel").hidden,
      null, { timeout: 20000 }
    );

    // The chat button, and the big box it opens on the board.
    await page.click(
      `.swarm-box[data-id="${which}"] .swarm-icon-button[data-does="chat"]`,
      { timeout: 20000 });
    await page.waitForSelector(
      `.swarm-chat-card[data-agent="${which}"] .swarm-chat-box`, { timeout: 20000 });
    const tall = await page.evaluate(
      (agent) => document.querySelector(
        `.swarm-chat-card[data-agent="${agent}"] .swarm-chat-box`).clientHeight,
      which);
    if (tall < 90) throw new Error(`the box to type in is only ${tall} tall`);
    console.log(`pass  the chat button opens a big box to type in (${tall} tall)`);

    const compactStop = page.locator(
      `.swarm-chat-card[data-agent="${which}"] .swarm-chat-stop`);
    await compactStop.waitFor({state: "visible", timeout: 20000});
    if (!(await compactStop.isDisabled())) {
      throw new Error("the compact Stop button is enabled when no request is running");
    }
    console.log("pass  the compact agent chat always exposes Stop");

    await page.getByRole("button", {name: "Open full Nexus chat"}).click();
    await page.waitForSelector("#theBigChat:not([hidden])", {timeout: 20000});
    const fullStop = page.locator("#theBigChatStop");
    if (!(await fullStop.isVisible()) || !(await fullStop.isDisabled())) {
      throw new Error("the maximised chat does not expose an idle Stop button");
    }
    console.log("pass  the maximised agent chat always exposes Stop");

    // This used to fail in three related ways: at a shorter desktop height the
    // composer was physically below the viewport, repeated background renders
    // rebuilt the whole chat, and Electron full-screen reparenting dropped the
    // caret. Keep one real draft focused while exercising all three paths.
    await nativeWindow.evaluate((window) => window.setContentSize(1280, 720));
    await page.waitForFunction(() => innerWidth >= 1200 && innerHeight >= 680,
      null, {timeout: 20000});
    const composerCheck = await page.evaluate(() => {
      const box = document.getElementById("theBigChatBox");
      box.value = "maximised composer regression";
      box.dispatchEvent(new Event("input", {bubbles: true}));
      box.focus();
      box.setSelectionRange(10, 18, "forward");
      const side = document.getElementById("theBigChatConversationList").firstElementChild;
      const destination = document.getElementById("theBigChatDestination").firstElementChild;
      const turn = document.getElementById("theBigChatSaid").firstElementChild;
      const started = performance.now();
      for (let count = 0; count < 50; count += 1) renderTheBigChat();
      const rect = box.getBoundingClientRect();
      const hit = document.elementFromPoint(rect.left + rect.width / 2,
        rect.top + rect.height / 2);
      return {
        elapsed: performance.now() - started,
        fullyVisible: rect.top >= 0 && rect.bottom <= innerHeight,
        hit: hit === box || box.contains(hit),
        focused: document.activeElement === box,
        value: box.value,
        selection: [box.selectionStart, box.selectionEnd],
        stableNodes:
          side === document.getElementById("theBigChatConversationList").firstElementChild
          && destination === document.getElementById("theBigChatDestination").firstElementChild
          && turn === document.getElementById("theBigChatSaid").firstElementChild,
      };
    });
    if (!composerCheck.fullyVisible || !composerCheck.hit || !composerCheck.focused
        || composerCheck.value !== "maximised composer regression"
        || composerCheck.selection.join(",") !== "10,18" || !composerCheck.stableNodes) {
      throw new Error(`the maximised composer lost its layout or state: ${JSON.stringify(composerCheck)}`);
    }
    await page.click("#theBigChatBox");
    await page.keyboard.press("Control+End");
    await page.keyboard.type("!");
    await page.evaluate(() => {
      const box = document.getElementById("theBigChatBox");
      box.focus();
      box.setSelectionRange(4, 12, "forward");
      box.dispatchEvent(new Event("select", {bubbles: true}));
    });
    await page.evaluate(() => toggleTheSwarmFullScreen());
    await page.waitForFunction(() => document.getElementById("swarmStage")
      .classList.contains("is-fullscreen"), null, {timeout: 20000});
    await page.evaluate(() => toggleTheSwarmFullScreen());
    await page.waitForFunction(() => !document.getElementById("swarmStage")
      .classList.contains("is-fullscreen"), null, {timeout: 20000});
    const afterReparent = await page.evaluate(() => {
      const box = document.getElementById("theBigChatBox");
      return {focused: document.activeElement === box, value: box.value,
        selection: [box.selectionStart, box.selectionEnd]};
    });
    if (!afterReparent.focused || !afterReparent.value.endsWith("!")
        || afterReparent.selection.join(",") !== "4,12") {
      throw new Error(`full-screen reparenting lost the maximised draft: ${JSON.stringify(afterReparent)}`);
    }
    await page.fill("#theBigChatBox", "");
    console.log(`pass  the maximised composer stays visible, clickable, focused, and cheap to refresh (${Math.round(composerCheck.elapsed)}ms/50)`);

    // Metadata and transcript arrive through separate HTTP reads. Exercise
    // their real renderer with deliberately crossed responses: a newly
    // selected title must never retain the old chat's words, and an older list
    // response must not restore the selection after a newer one has landed.
    await page.evaluate(async (agentId) => {
      const held = swarmChats.find((one) => one.agent === agentId);
      if (!held) throw new Error("the smoke chat state disappeared");
      const original = {
        conversations: held.conversations,
        conversation: held.conversation,
        said: held.said,
        saidFor: held.saidFor,
      };
      const originalRequest = request;
      const agent = theSwarmAgent(agentId);
      const board = theSwarmBoard();
      const originalAgents = board.agents;
      const syntheticPeer = {
        id: "synthetic-connected-peer", name: "Synthetic connected peer",
        who: "synthetic-peer", ready: false,
        why_not: "Synthetic provider is unavailable for this deterministic smoke.",
      };
      board.agents = [...(originalAgents || []), syntheticPeer];
      const conversation = (id, name) => ({
        id, name, pair: [agentId], pair_agents: [{id: agentId, name: agent.name}],
        projects: [], project: "", destination: {
          owner_label: "Nexus Harness", connected: true,
          provider_label: "Smoke route", route: "smoke", model: "",
          transcript_path: "", transcript_exists: false,
          explanation: `Synthetic ${name} destination.`,
        },
      });
      const alpha = conversation("smoke-chat-alpha", "Alpha chat");
      const beta = conversation("smoke-chat-beta", "Beta chat");
      try {
        nextConversationListRevision(agentId);
        held.conversations = [alpha, beta];
        held.conversation = alpha.id;
        held.saidFor = alpha.id;
        held.said = [{who: "them", text: "ALPHA TRANSCRIPT MARKER", at: ""}];
        renderTheBigChat();

        const directNew = [...document.querySelectorAll(
          "#theBigChatConversationList .the-big-chat-pair-new"
        )].find((button) => button.textContent === "+ New chat for this agent");
        if (!directNew) {
          throw new Error("a saved lone-agent group has no New chat control");
        }
        const originalCreateConversationFor = createConversationFor;
        const directCreationCalls = [];
        try {
          createConversationFor = async (...args) => { directCreationCalls.push(args); };
          directNew.click();
          await Promise.resolve();
          alpha.binding_problem = {
            message: "Synthetic direct-chat binding changed.",
            action_label: "Start fresh with current setup",
          };
          renderTheBigChat();
          const directRepair = document.querySelector(
            "#theBigChatConversationList .the-big-chat-binding-repair button"
          );
          if (!directRepair) {
            throw new Error("a saved lone-agent binding problem has no fresh-chat control");
          }
          directRepair.click();
          await Promise.resolve();
        } finally {
          createConversationFor = originalCreateConversationFor;
          delete alpha.binding_problem;
          renderTheBigChat();
        }
        if (directCreationCalls.length !== 2 || directCreationCalls.some((args) => (
          args[0] !== agentId || args[1] !== "" || args[2] !== "single"
        ))) {
          throw new Error(
            `lone-agent create/repair changed conversation scope: ${JSON.stringify(directCreationCalls)}`
          );
        }

        setWhatCanBePressedInSwarm();
        const activity = document.getElementById("theBigChatActivity");
        if (activity.parentElement?.className !== "the-big-chat-transcript"
            || activity !== activity.parentElement.lastElementChild) {
          throw new Error("chat progress is not anchored inside the transcript bottom");
        }
        if (!document.getElementById("theBigChatCollaborate").disabled
            || !document.getElementById("theBigChatWork").disabled
            || document.getElementById("theBigChatSend").disabled
            || document.getElementById("theBigChatAttach").disabled) {
          throw new Error("a lone-agent chat exposes the wrong set of actions");
        }
        if (document.getElementById("theBigChatUnlimited").checked
            || document.getElementById("theBigChatRoundLimit").value !== "3") {
          throw new Error("new chats do not start with the finite three-round collaboration limit");
        }
        const project = (theSwarmBoard().projects || []).find((one) => one.is_there);
        alpha.pair = [agentId, "synthetic-connected-peer"];
        alpha.pair_agents = [
          {id: agentId, name: agent.name, who: agent.who, ready: true},
          {id: "synthetic-connected-peer", name: "Synthetic connected peer",
            who: "synthetic-peer", ready: false},
        ];
        if (project) {
          alpha.project = project.id;
          alpha.projects = [{id: project.id, name: project.name, is_there: true}];
        }
        setWhatCanBePressedInSwarm();
        if (!document.getElementById("theBigChatCollaborate").disabled
            || !document.getElementById("theBigChatTeamReadiness").textContent
              .includes("Repair Synthetic connected peer")) {
          throw new Error("an unavailable pair participant is not named with its Repair action");
        }
        alpha.pair_agents[1].ready = true;
        syntheticPeer.ready = true;
        setWhatCanBePressedInSwarm();
        if (document.getElementById("theBigChatCollaborate").disabled) {
          throw new Error("a connected-pair chat incorrectly disables collaboration");
        }
        if (document.getElementById("theBigChatSend").textContent
              !== `Ask ${agent.name} only`
            || document.getElementById("theBigChatCollaborate").textContent
              !== "Ask both agents"
            || !document.getElementById("theBigChatScopeHint").textContent
              .includes("expected initial replies: 2")) {
          throw new Error("the chat does not name direct versus two-agent recipients explicitly");
        }
        const originalSendFromTheBigChat = sendFromTheBigChat;
        let workMode = "";
        try {
          sendFromTheBigChat = async (mode) => { workMode = mode; };
          document.getElementById("theBigChatWork").disabled = false;
          document.getElementById("theBigChatWork").click();
        } finally {
          sendFromTheBigChat = originalSendFromTheBigChat;
        }
        if (workMode !== "work") {
          throw new Error("the Work together on project files button has no work action");
        }
        alpha.pair = [agentId];
        alpha.pair_agents = [{id: agentId, name: agent.name}];
        alpha.project = "";
        alpha.projects = [];

        applyConversationList(agentId, {active: beta.id, chats: [alpha, beta]});
        const visibleAfterSwitch = document.getElementById("theBigChatSaid").textContent;
        if (!document.getElementById("theBigChatTitle").textContent.includes("Beta chat")
            || visibleAfterSwitch.includes("ALPHA TRANSCRIPT MARKER")) {
          throw new Error("a new chat title was rendered with the previous transcript");
        }

        held.conversation = alpha.id;
        held.saidFor = alpha.id;
        held.said = [];
        renderTheBigChat();
        let releaseOld;
        let releaseNew;
        const oldList = new Promise((resolve) => { releaseOld = resolve; });
        const newList = new Promise((resolve) => { releaseNew = resolve; });
        let listReads = 0;
        request = async (url, options) => {
          if (url.startsWith("/api/swarm/chats?")) {
            listReads += 1;
            return listReads === 1 ? oldList : newList;
          }
          return originalRequest(url, options);
        };
        const olderRead = loadConversationsFor(agentId, false);
        const newerRead = loadConversationsFor(agentId, false);
        releaseNew({active: beta.id, chats: [alpha, beta]});
        await newerRead;
        releaseOld({active: alpha.id, chats: [alpha, beta]});
        await olderRead;
        if (held.conversation !== beta.id) {
          throw new Error("an older conversation-list response restored the previous selection");
        }

        keepWhatWasSaidTo(agentId,
          [{who: "them", text: "STALE ALPHA ANSWER", at: ""}], alpha.id);
        keepWhatWasSaidTo(agentId,
          [{who: "them", text: "BETA TRANSCRIPT MARKER", at: ""}], beta.id);
        const finalWords = document.getElementById("theBigChatSaid").textContent;
        if (!finalWords.includes("BETA TRANSCRIPT MARKER")
            || finalWords.includes("STALE ALPHA ANSWER")) {
          throw new Error("a stale transcript response crossed into the selected chat");
        }

        // A participant outcome is an append-only transcript turn, not a
        // transient toast. Rebuild the real maximised renderer as though the
        // desktop had restarted, then exercise only local recovery actions.
        alpha.pair = [agentId, "synthetic-connected-peer"];
        alpha.pair_agents = [
          {id: agentId, name: agent.name, who: agent.who, ready: true},
          {id: "synthetic-connected-peer", name: "Synthetic connected peer",
            who: "synthetic-peer", ready: true},
        ];
        held.conversation = alpha.id;
        held.saidFor = alpha.id;
        held.said = [
          {who: "you", speaker_name: "You", text: "ORIGINAL TEAM PROMPT",
            phase: "user_prompt"},
          {who: "them", speaker_id: "synthetic-connected-peer",
            speaker_name: "Synthetic connected peer", text: "A saved answer",
            phase: "final_answer"},
          {who: "them", speaker_id: "nexus", speaker_name: "Nexus",
            recipient_name: "You", text: "1 of 2 agents answered.",
            phase: "participant_outcome", participant_outcome: {
              schema_version: 1, outcome: "partial", requested_mode: "collaborate",
              expected_count: 2, answered_count: 1,
              participants: [
                {agent_id: agentId, name: agent.name, route: agent.who,
                  status: "failed", answer_saved: false,
                  provider_reason: "synthetic provider failure"},
                {agent_id: "synthetic-connected-peer", name: "Synthetic connected peer",
                  route: "synthetic-peer", status: "answered", answer_saved: true},
              ],
              actions: [{id: "repair-provider", agent_id: agentId,
                route: agent.who, label: `Repair ${agent.name}`}],
            }},
        ];
        document.getElementById("theBigChatBox").value = "";
        renderTheBigChat();
        const outcome = document.querySelector(
          '#theBigChatSaid .participant-outcome-card[data-outcome="partial"]');
        if (!outcome || !outcome.textContent.includes("1 of 2 agents answered")
            || !outcome.textContent.includes(`Repair ${agent.name}`)
            || !outcome.textContent.includes("Ask all agents again")) {
          throw new Error("the durable one-of-two participant outcome did not render its recovery actions");
        }
        const originalRepair = openAgentRepairFlow;
        let repairedAgent = "";
        try {
          openAgentRepairFlow = async (id) => { repairedAgent = id; };
          outcome.querySelector(".participant-outcome-repair").click();
          await Promise.resolve();
        } finally {
          openAgentRepairFlow = originalRepair;
        }
        if (repairedAgent !== agentId) {
          throw new Error("participant repair targeted a different agent");
        }
        const requestBeforeLocalRetry = request;
        let retryRequests = 0;
        try {
          request = async () => { retryRequests += 1; throw new Error("must not send"); };
          outcome.querySelector(".participant-outcome-ask-again").click();
        } finally {
          request = requestBeforeLocalRetry;
        }
        if (retryRequests !== 0
            || document.getElementById("theBigChatBox").value !== "ORIGINAL TEAM PROMPT"
            || !outcome.textContent.includes("Nothing was sent")) {
          throw new Error("Ask all agents again did not remain a review-only composer restore");
        }

        const outcomeTurn = held.said[2];
        outcomeTurn.text = "0 of 2 agents answered.";
        outcomeTurn.participant_outcome = {
          schema_version: 1, outcome: "none", requested_mode: "collaborate",
          expected_count: 2, answered_count: 0,
          participants: [
            {agent_id: agentId, name: agent.name, route: "obsolete-smoke-route",
              status: "outcome_unknown", answer_saved: false,
              outcome_unknown: true, provider_reason: "synthetic uncertain delivery"},
            {agent_id: "synthetic-connected-peer", name: "Synthetic connected peer",
              route: "synthetic-peer", status: "failed", answer_saved: false},
          ],
          actions: [
            {id: "inspect-provider-turn", agent_id: agentId,
              route: "obsolete-smoke-route", label: `Inspect ${agent.name}`},
            {id: "repair-provider", agent_id: "synthetic-connected-peer",
              route: "synthetic-peer", label: "Repair Synthetic connected peer"},
          ],
        };
        renderTheBigChat();
        const zeroOutcome = document.querySelector(
          '#theBigChatSaid .participant-outcome-card[data-outcome="none"]');
        if (!zeroOutcome || !zeroOutcome.textContent.includes("0 of 2 agents answered")
            || !zeroOutcome.textContent.includes("No AI answer was saved")
            || !zeroOutcome.textContent.includes("Nexus will not resend")
            || zeroOutcome.querySelector(".participant-outcome-ask-again")) {
          throw new Error("the zero-answer unknown-delivery card permits an unsafe resend");
        }
        const originalChangedRouteRepair = openAgentRepairFlow;
        let changedRouteRepairTarget = "";
        try {
          openAgentRepairFlow = async (id) => { changedRouteRepairTarget = id; };
          zeroOutcome.querySelector(".participant-outcome-repair").click();
          await Promise.resolve();
        } finally {
          openAgentRepairFlow = originalChangedRouteRepair;
        }
        if (changedRouteRepairTarget) {
          throw new Error("a changed participant route diagnosed the replacement connection");
        }

        outcomeTurn.text = "2 of 2 agents answered.";
        outcomeTurn.participant_outcome = {
          schema_version: 1, outcome: "complete", requested_mode: "collaborate",
          expected_count: 2, answered_count: 2,
          participants: [
            {agent_id: agentId, name: agent.name, route: agent.who,
              status: "answered", answer_saved: true},
            {agent_id: "synthetic-connected-peer", name: "Synthetic connected peer",
              route: "synthetic-peer", status: "answered", answer_saved: true},
          ], actions: [],
        };
        renderTheBigChat();
        const completeOutcome = document.querySelector(
          '#theBigChatSaid .participant-outcome-card[data-outcome="complete"]');
        if (!completeOutcome || !completeOutcome.textContent.includes("2 of 2 agents answered")
            || completeOutcome.querySelector(".participant-outcome-repair")
            || completeOutcome.querySelector(".participant-outcome-ask-again")) {
          throw new Error("the complete participant outcome exposes unnecessary recovery controls");
        }

        // The integrity-recovery control must remain scoped to the exact saved
        // chat and must not share any path with provider dispatch. Exercise the
        // real packaged renderer/button while replacing only its local HTTP
        // boundary; the server endpoint has its own full HTTP integration test.
        alpha.collaboration_problem = {
          schema_version: 1,
          code: "collaboration_record_untrusted",
          message: "Synthetic collaboration record needs recovery.",
          action: "reset_collaboration_record",
          action_label: "Reset collaboration record",
          preserves_transcript: true,
          automatic_resend: false,
        };
        renderTheBigChat();
        const resetButton = document.querySelector(
          "#theBigChatDestination .collaboration-record-reset");
        if (!resetButton || !resetButton.textContent.includes("Reset collaboration record")) {
          throw new Error("the scoped collaboration reset is not visible for an untrusted record");
        }
        const requestBeforeReset = request;
        const loadBeforeReset = loadConversationsFor;
        const refreshBeforeReset = refreshTheChatFor;
        const confirmBeforeReset = window.confirm;
        const resetCalls = [];
        let confirmation = "";
        try {
          window.confirm = (message) => { confirmation = String(message || ""); return true; };
          request = async (url, options = {}) => {
            if (url !== "/api/swarm/collaboration/reset") {
              throw new Error(`collaboration reset made an unexpected request: ${url}`);
            }
            resetCalls.push({url, body: JSON.parse(options.body || "{}")});
            return {note: "Synthetic record reset without provider contact."};
          };
          loadConversationsFor = async () => {};
          refreshTheChatFor = async () => {};
          resetButton.click();
          for (let attempt = 0; attempt < 20 && resetCalls.length === 0; attempt += 1) {
            await new Promise((resolve) => setTimeout(resolve, 0));
          }
        } finally {
          request = requestBeforeReset;
          loadConversationsFor = loadBeforeReset;
          refreshTheChatFor = refreshBeforeReset;
          window.confirm = confirmBeforeReset;
          delete alpha.collaboration_problem;
        }
        if (resetCalls.length !== 1
            || resetCalls[0].body.agent !== agentId
            || resetCalls[0].body.chat !== alpha.id
            || !confirmation.includes("transcript, attachments, and provider conversations are preserved")
            || !confirmation.includes("No prompt is sent")) {
          throw new Error("the collaboration reset was not exact, explicit, and provider-free");
        }
      } finally {
        request = originalRequest;
        board.agents = originalAgents;
        held.conversations = original.conversations;
        held.conversation = original.conversation;
        held.said = original.said;
        held.saidFor = original.saidFor;
        nextConversationListRevision(agentId);
        nextSwarmChatRevision(agentId);
        renderTheChatThreadFor(agentId, keptTranscriptFor(agentId));
        renderTheBigChat();
      }
    }, which);
    console.log("pass  selected chat, title, project, and transcript stay atomic under stale reads");
    console.log("pass  lone-chat actions and in-transcript progress follow the selected chat type");
    console.log("pass  lone-chat New and repair controls preserve the exact one-agent scope");
    console.log("pass  direct/team labels, finite rounds, readiness repair, and durable complete/partial/zero outcomes work without live providers");
    console.log("pass  damaged collaboration recovery resets only the exact record and sends no provider prompt");
    await page.click("#theBigChatSmall");

    await page.fill(`.swarm-chat-card[data-agent="${which}"] .swarm-chat-box`,
      "Typed by a smoke check");
    await page.waitForFunction(
      (agent) => document.querySelector(
        `.swarm-chat-card[data-agent="${agent}"] .swarm-chat-count`)
        .textContent === "22 / 200,000 characters",
      which, { timeout: 20000 }
    );
    console.log("pass  what is typed into it really goes in");

    // Minimise is different from close: the card leaves the board, while its
    // tray button keeps the conversation open and can bring it back big.
    await page.click(
      `.swarm-chat-card[data-agent="${which}"] .swarm-icon-button[data-does="minimise"]`,
      { timeout: 20000 });
    await page.waitForFunction(
      (agent) => !document.querySelector(`.swarm-chat-card[data-agent="${agent}"]`)
        && document.querySelector(`[data-chat-tray="${agent}"]`),
      which, { timeout: 20000 }
    );
    console.log("pass  the chat minimises into the tray");

    // The agent's chat button restores the board card; close then removes the
    // conversation from both the board and the tray.
    await page.click(
      `.swarm-box[data-id="${which}"] .swarm-icon-button[data-does="chat"]`,
      { timeout: 20000 });
    await page.waitForSelector(
      `.swarm-chat-card[data-agent="${which}"]`, { timeout: 20000 });

    await page.click(
      `.swarm-chat-card[data-agent="${which}"] .swarm-icon-button[data-does="close"]`,
      { timeout: 20000 });
    await page.waitForFunction(
      (agent) => !document.querySelector(`.swarm-chat-card[data-agent="${agent}"]`),
      which, { timeout: 20000 }
    );
    console.log("pass  and the chat closes again");

    // Removing an agent deliberately asks for confirmation. Accept the native
    // Electron dialog just as this smoke check does for automation deletion;
    // otherwise Playwright dismisses it and the temporary agent is left on
    // the restored board until the finally block repairs the saved snapshot.
    page.once("dialog", (dialog) => dialog.accept());
    await page.click("#swarmAgentRemove", { timeout: 20000 });
    try {
      await page.waitForFunction(
        (agent) => !document.querySelector(`.swarm-box.agent[data-id="${agent}"]`),
        which, { timeout: 30000 }
      );
      await page.waitForFunction(async (agent) => (
        !(await request("/api/swarm")).board.agents.some((one) => one.id === agent)
      ), which, { timeout: 30000 });
    } catch (error) {
      const state = await page.evaluate(async () => ({
        picked: swarmPicked,
        visibleIds: [...document.querySelectorAll(".swarm-box.agent")]
          .map((one) => one.dataset.id),
        savedIds: (await request("/api/swarm")).board.agents.map((one) => one.id),
        note: document.getElementById("swarmSaid").textContent,
      }));
      throw new Error(`agent removal did not settle: ${JSON.stringify(state)}; ${error.message}`);
    }
    console.log("pass  Remove this agent takes it off again");
  } finally {
    if (putBack) {
      try {
        await page.evaluate(async (board) => {
          const now = (await request("/api/swarm")).board.version;
          await request("/api/swarm/save", {
            method: "POST",
            body: JSON.stringify({ board: Object.assign({}, board, { version: now }) }),
          });
        }, putBack);
        console.log("pass  and the board this machine had is back");
      } catch (error) {
        console.error(`The board could not be put back: ${error.message}`);
      }
    }
    await app.close().catch(() => {});
  }
  console.log("\nThe agent board works in the app somebody installs.");
}

main().catch((error) => {
  console.error(`\n${(error && error.message) || error}`);
  process.exit(1);
});
