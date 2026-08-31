"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const APP = fs.readFileSync(
  path.join(__dirname, "..", "src", "our_harness", "ui", "app.js"), "utf8",
);
const WEB_CHAT_SHELL = fs.readFileSync(
  path.join(__dirname, "pages", "web-chat.html"), "utf8",
);
const INDEX = fs.readFileSync(
  path.join(__dirname, "..", "src", "our_harness", "ui", "index.html"), "utf8",
);
const PRELOAD = fs.readFileSync(path.join(__dirname, "preload.js"), "utf8");
const MAIN = fs.readFileSync(path.join(__dirname, "main.js"), "utf8");

test("provider shell reports only the durable local save until board assignment confirms", () => {
  assert.match(WEB_CHAT_SHELL, /was saved as a Nexus web chat/);
  assert.doesNotMatch(WEB_CHAT_SHELL, /was sent to the Nexus board/);
  assert.match(WEB_CHAT_SHELL, /await window\.nexusWebChatWindow\.startNew\(\)/);
});

test("web chat manager discloses computer scope and shared provider capacity", () => {
  assert.match(INDEX, /Chats connected on this computer/);
  assert.match(INDEX, /same provider share this\s+one Nexus sign-in, account, and quota/);
  assert.match(INDEX, /Nexus relays them one at a time/);
});

test("desktop reconnect IPC forwards the exact route and conversation binding", async () => {
  let exposed = null;
  const calls = [];
  vm.runInNewContext(PRELOAD, {
    require: (name) => {
      assert.equal(name, "electron");
      return {
        contextBridge: {exposeInMainWorld: (_name, value) => { exposed = value; }},
        ipcRenderer: {
          invoke: (...args) => { calls.push(args); return Promise.resolve(true); },
          on: () => {},
        },
      };
    },
  });

  await exposed.connectWebChat(
    "chatgpt", "chatgpt-abcdef123456", "portable-board-chat-17", true,
  );
  assert.deepEqual(calls.at(-1), [
    "harness:webChatConnect", "chatgpt", "chatgpt-abcdef123456",
    "portable-board-chat-17", true,
  ]);
  assert.match(
    MAIN,
    /webChatManager\.openSetup\(\s*String\(provider \|\| ""\), exactId, String\(conversationKey \|\| ""\),\s*Boolean\(preferExisting\)\)/,
  );
});

test("repair action reopens the exact saved web-chat route and conversation", async () => {
  const start = APP.indexOf("async function performAgentRepairAction");
  const end = APP.indexOf("async function runAgentRouteTest", start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  const connected = [];
  let providerRefreshes = 0;
  let conversationKey = "portable-board-chat-17";
  let preferExisting = true;
  const elements = {};
  const context = {
    $: (id) => (elements[id] ||= {textContent: "", focus: () => {}}),
    activeConversationFor: () => ({
      filed_as: "fallback-key",
      destination: {
        web_conversation_key: conversationKey,
        web_prefer_existing_conversation: preferExisting,
      },
    }),
    webChatProviderChoices: [],
    refreshWebChatProviderChoices: async () => {
      providerRefreshes += 1;
      return [{id: "chatgpt", label: "ChatGPT"}];
    },
    pickSwarmBox: () => {},
    connectPickedAgentToWebProvider: async (...args) => { connected.push(args); return true; },
    openWebChatManager: async () => {},
    window: {harnessDesktop: {}},
  };
  vm.runInNewContext(
    `${APP.slice(start, end)}\nthis.repairUnderTest = performAgentRepairAction;`,
    context,
  );

  await context.repairUnderTest(
    "agent-17", "web:chatgpt-abcdef123456",
    {id: "web-chat", provider: "chatgpt", connection_id: "chatgpt-abcdef123456"},
    {},
  );

  assert.equal(providerRefreshes, 1);
  assert.equal(connected.length, 1);
  assert.equal(connected[0][0], "chatgpt");
  assert.equal(connected[0][1].connectionId, "chatgpt-abcdef123456");
  assert.equal(connected[0][1].conversationKey, "portable-board-chat-17");
  assert.equal(connected[0][1].preferExisting, true);

  conversationKey = "portable-board-chat-17-restart-fresh";
  preferExisting = false;
  await context.repairUnderTest(
    "agent-17", "web:chatgpt-abcdef123456",
    {id: "web-chat", provider: "chatgpt", connection_id: "chatgpt-abcdef123456"},
    {},
  );
  assert.equal(providerRefreshes, 2);
  assert.equal(connected[1][1].conversationKey, "portable-board-chat-17-restart-fresh");
  assert.equal(connected[1][1].preferExisting, false);
});

test("renderer connect intent keeps the exact ID and key through desktop IPC", async () => {
  const start = APP.indexOf("async function connectPickedAgentToWebProvider");
  const end = APP.indexOf("async function assignSelectedWebChatToPendingAgent", start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  const calls = [];
  const context = {
    thePickedAgent: () => ({id: "agent-17"}),
    webChatProviderChoices: [{id: "chatgpt", label: "ChatGPT"}],
    renderSwarmAgentPanel: () => {},
    sayInSwarm: () => {},
    window: {harnessDesktop: {
      connectWebChat: async (...args) => { calls.push(args); return true; },
    }},
  };
  vm.runInNewContext(
    `let webChatAssignTarget = null;
     ${APP.slice(start, end)}
     this.connectUnderTest = connectPickedAgentToWebProvider;
     this.targetUnderTest = () => webChatAssignTarget;`,
    context,
  );

  const worked = await context.connectUnderTest("chatgpt", {
    connectionId: "chatgpt-abcdef123456",
    conversationKey: "portable-board-chat-17",
    preferExisting: true,
  });

  assert.equal(worked, true);
  assert.deepEqual(calls[0], [
    "chatgpt", "chatgpt-abcdef123456", "portable-board-chat-17", true,
  ]);
  assert.equal(context.targetUnderTest().connectionId, "chatgpt-abcdef123456");
  assert.equal(context.targetUnderTest().conversationKey, "portable-board-chat-17");
});

test("successful exact reconnect leaves the agent route unchanged", async () => {
  const start = APP.indexOf("async function assignSelectedWebChatToPendingAgent");
  const end = APP.indexOf("function acceptLocalWebChatConnections", start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  let boardWrites = 0;
  const context = {
    theSwarmAgent: () => ({
      id: "agent-17", who: "web:chatgpt-abcdef123456",
    }),
    changeTheSwarmBoard: async () => { boardWrites += 1; return true; },
    pickSwarmBox: () => {},
    sayInSwarm: () => {},
    renderWebChatConnections: () => {},
  };
  vm.runInNewContext(
    `let webChatAssignTarget = {
       agentId: "agent-17", providerId: "chatgpt",
       connectionId: "chatgpt-abcdef123456",
       conversationKey: "portable-board-chat-17"
     };
     ${APP.slice(start, end)}
     this.assignUnderTest = assignSelectedWebChatToPendingAgent;
     this.targetUnderTest = () => webChatAssignTarget;`,
    context,
  );

  const result = await context.assignUnderTest({
    id: "chatgpt-abcdef123456", provider: "chatgpt", title: "Reconnected helper",
  });

  assert.equal(result.matched, true);
  assert.equal(result.worked, true);
  assert.equal(boardWrites, 0);
  assert.equal(context.targetUnderTest(), null);
});

test("saved-chat reset attempts every old key and reloads after cleanup failure", async () => {
  const start = APP.indexOf("async function startTheChatAgainFor");
  const end = APP.indexOf("async function refreshWhatTheySaidToEachOther", start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  const resetCalls = [];
  const notices = [];
  const shownErrors = [];
  let reloads = 0;
  const context = {
    theSwarmAgent: () => ({id: "agent-cli", name: "CLI lead", who: "claude"}),
    swarmChatIsHydrating: () => false,
    swarmConversationSwitching: new Set(),
    activeConversationFor: () => ({id: "chat-portable-17"}),
    swarmChatRuntimeKeyFor: () => "runtime-chat-portable-17",
    swarmBusy: new Set(),
    swarmChatResetting: new Set(),
    sayInRuntimeChat: (_key, words) => { notices.push(words); },
    nextSwarmChatRevision: () => {},
    setWhatCanBePressedInSwarm: () => {},
    request: async () => ({
      note: "started again",
      web_chat_id: "chatgpt-portable-17",
      web_conversation_key: "new-provider-key",
      previous_web_conversation_key: "old-provider-key",
      web_chat_resets: [
        {route: "web:chatgpt-portable-17", previous_web_conversation_key: "old-provider-key"},
        {route: "web:gemini-portable-18", previous_web_conversation_key: "old-provider-key"},
      ],
    }),
    keepWhatWasSaidToRuntime: () => {},
    loadConversationsFor: async () => { reloads += 1; },
    showError: (words) => { shownErrors.push(words); },
    window: {harnessDesktop: {
      resetWebChat: async (...args) => {
        resetCalls.push(args);
        if (args[0].includes("chatgpt")) throw new Error("settings copy is busy");
        return true;
      },
    }},
  };
  vm.runInNewContext(
    `${APP.slice(start, end)}\nthis.resetUnderTest = startTheChatAgainFor;`,
    context,
  );

  await context.resetUnderTest("agent-cli");

  assert.deepEqual(resetCalls, [
    ["web:chatgpt-portable-17", "old-provider-key"],
    ["web:gemini-portable-18", "old-provider-key"],
  ]);
  assert.equal(reloads, 1);
  assert.deepEqual(shownErrors, []);
  assert.match(notices.at(-1), /started again/);
  assert.match(notices.at(-1), /could not remove one old provider-window mapping/);
  assert.match(notices.at(-1), /settings copy is busy/);
  assert.equal(context.swarmChatResetting.size, 0);
});

function recoveryHelpers({initialStatus, resolution, resolutionError} = {}) {
  const start = APP.indexOf("function webChatRecoveryNeedsChoice");
  const end = APP.indexOf("async function heartbeatWebChats", start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  const elements = {};
  const element = (id) => {
    if (!elements[id]) elements[id] = {
      id, hidden: false, disabled: false, textContent: "", dataset: {},
    };
    return elements[id];
  };
  const calls = {status: 0, resolve: [], accepted: [], refresh: 0, heartbeat: 0};
  const context = {
    $: element,
    window: {harnessDesktop: {
      desktopSettingsRecoveryStatus: async () => {
        calls.status += 1;
        return initialStatus;
      },
      resolveDesktopSettingsRecovery: async (action) => {
        calls.resolve.push(action);
        if (resolutionError) throw resolutionError;
        return resolution;
      },
    }},
    acceptLocalWebChatConnections: (connections) => calls.accepted.push(connections),
    refreshLocalWebChatConnections: async () => { calls.refresh += 1; return []; },
    renderWebChatConnections: () => {},
    heartbeatWebChats: async () => { calls.heartbeat += 1; },
  };
  vm.runInNewContext(
    `let webChatSettingsRecoveryStatus = null;
     let webChatSettingsRecoveryBusy = false;
     let webChatSettingsRecoveryReadError = "";
     ${APP.slice(start, end)}
     this.refreshRecovery = refreshWebChatSettingsRecoveryStatus;
     this.resolveRecovery = resolveWebChatSettingsRecovery;`,
    context,
  );
  return {...context, elements, calls};
}

test("settings recovery is explicit, preserves other settings, and refreshes routes", async () => {
  const pending = {
    state: "copies_disagree", resolution_required: true,
    requires_web_chat_resolution: true, recovered_web_chat_count: 2,
    copies_disagree: true, reason: "The backup contains the last complete chat list.",
  };
  const harness = recoveryHelpers({
    initialStatus: pending,
    resolution: {
      status: {state: "ok", resolution_required: false, recovered_web_chat_count: 0},
      changed: true, connections: [],
    },
  });

  await harness.refreshRecovery();
  assert.deepEqual(harness.calls.resolve, [], "reading status must never auto-resolve it");
  assert.equal(harness.elements.webChatRecoveryBanner.hidden, false);
  assert.equal(harness.elements.desktopSettingsRecoveryCard.dataset.state, "attention");
  assert.match(harness.elements.webChatSettingsRecoveryMessage.textContent, /quarantined 2/);
  assert.match(harness.elements.webChatSettingsRecoveryMessage.textContent, /backup contains/);

  assert.equal(await harness.resolveRecovery("discard_web_chats"), true);
  assert.deepEqual(harness.calls.resolve, ["discard_web_chats"]);
  assert.equal(harness.calls.refresh, 1);
  assert.equal(harness.calls.heartbeat, 1);
  assert.equal(harness.elements.webChatRecoveryBanner.hidden, true);
  assert.match(harness.elements.desktopSettingsRecoveryResult.textContent, /other desktop settings were preserved/);

  const restoring = recoveryHelpers({
    initialStatus: pending,
    resolution: {
      status: {state: "ok", resolution_required: false, recovered_web_chat_count: 0},
      changed: true, connections: [{id: "restored-one"}, {id: "restored-two"}],
      restored_connection_count: 2,
    },
  });
  await restoring.refreshRecovery();
  assert.equal(await restoring.resolveRecovery("restore"), true);
  assert.match(restoring.elements.desktopSettingsRecoveryResult.textContent,
    /Restored 2 usable web AI chats/);

  const unusable = recoveryHelpers({
    initialStatus: pending,
    resolution: {
      status: {state: "ok", resolution_required: false, recovered_web_chat_count: 0},
      changed: true, connections: [], restored_connection_count: 0,
    },
  });
  await unusable.refreshRecovery();
  assert.equal(await unusable.resolveRecovery("restore"), true);
  assert.match(unusable.elements.desktopSettingsRecoveryResult.textContent,
    /No usable web AI chat routes were restored/);
  assert.match(unusable.elements.desktopSettingsRecoveryResult.textContent,
    /ignored 2 saved entries/);
});

test("zero-chat settings disagreement offers repair and a failed choice stays visible", async () => {
  const pending = {
    state: "copies_disagree", resolution_required: true,
    requires_web_chat_resolution: false, recovered_web_chat_count: 0,
    copies_disagree: true, reason: "The primary and backup revisions differ.",
  };
  const harness = recoveryHelpers({
    initialStatus: pending,
    resolutionError: new Error("settings volume is read-only"),
  });

  await harness.refreshRecovery();
  assert.equal(harness.elements.webChatRecoveryBannerAction.textContent, "Repair settings copies");
  assert.equal(harness.elements.desktopSettingsRecoveryRestore.textContent, "Repair settings copies");
  assert.equal(harness.elements.desktopSettingsRecoveryDiscard.hidden, true);
  assert.equal(await harness.resolveRecovery("restore"), false);
  assert.equal(harness.elements.webChatRecoveryBanner.hidden, false);
  assert.match(harness.elements.desktopSettingsRecoveryResult.textContent, /Nothing was changed/);
  assert.match(harness.elements.desktopSettingsRecoveryResult.textContent, /read-only/);
});

test("unreadable copies offer repair and a concurrent resolution is reported truthfully", async () => {
  const pending = {
    state: "recovery_pending", resolution_required: true,
    requires_web_chat_resolution: false, recovered_web_chat_count: 0,
    // A startup preference write can already have carried the incident into a
    // valid primary envelope. The reason must keep the no-readable-copy wording.
    copies_disagree: true, selected_source: "primary", reason: "both_copies_invalid",
  };
  const harness = recoveryHelpers({
    initialStatus: pending,
    resolution: {
      status: {state: "ok", resolution_required: false, recovered_web_chat_count: 0},
      changed: false, connections: [{id: "already-restored-elsewhere"}],
      reload_error: "route refresh is temporarily busy",
    },
  });

  await harness.refreshRecovery();
  assert.match(harness.elements.webChatSettingsRecoveryMessage.textContent,
    /could not read a usable desktop settings copy/);
  assert.match(harness.elements.webChatSettingsRecoveryMessage.textContent,
    /creates a clean matching pair/);
  assert.equal(harness.elements.desktopSettingsRecoveryRestore.textContent,
    "Repair settings copies");

  assert.equal(await harness.resolveRecovery("restore"), true);
  const result = harness.elements.desktopSettingsRecoveryResult.textContent;
  assert.match(result, /already resolved by another Nexus window or an earlier attempt/);
  assert.match(result, /no additional settings were changed/);
  assert.match(result, /route refresh is temporarily busy/);
  assert.doesNotMatch(result, /Repaired the saved desktop settings copies/);
});

test("newer-format settings stay read-only with clear update guidance", async () => {
  const harness = recoveryHelpers({
    initialStatus: {
      state: "update_required", update_required: true, write_blocked: true,
      format_version: 1, supported_format_version: 1, found_format_versions: [3],
      resolution_required: false, requires_web_chat_resolution: false,
      recovered_web_chat_count: 0,
    },
  });

  await harness.refreshRecovery();
  assert.equal(harness.elements.webChatRecoveryBanner.hidden, false);
  assert.equal(harness.elements.webChatSettingsRecovery.hidden, false);
  assert.equal(harness.elements.webChatSettingsRecoveryTitle.textContent,
    "A newer Nexus version is required");
  assert.equal(harness.elements.webChatRecoveryBannerTitle.textContent,
    "A newer Nexus version is required");
  assert.match(harness.elements.desktopSettingsRecoveryMessage.textContent,
    /newer format version 3/);
  assert.match(harness.elements.desktopSettingsRecoveryMessage.textContent,
    /left both saved copies and their web-chat routes untouched/);
  assert.match(harness.elements.desktopSettingsRecoveryMessage.textContent, /read-only/);
  assert.match(harness.elements.webChatSettingsRecoveryHint.textContent,
    /cannot safely change or convert these files/);
  assert.equal(harness.elements.webChatBackgroundMode.disabled, true);
  assert.equal(harness.elements.desktopSettingsRecoveryRestore.hidden, true);
  assert.equal(harness.elements.desktopSettingsRecoveryDiscard.hidden, true);
  assert.equal(harness.elements.webChatRecoveryBannerAction.textContent, "See update steps");
  assert.deepEqual(harness.calls.resolve, []);
  assert.equal(await harness.resolveRecovery("restore"), false);
  assert.deepEqual(harness.calls.resolve, []);
});

function receiptHelpers(request) {
  const start = APP.indexOf("function webChatReceiptIsRetryable");
  const end = APP.indexOf("async function relayOneWebChatRequest", start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  const context = {
    request,
    window: {setTimeout: (callback) => callback()},
  };
  vm.runInNewContext(
    `${APP.slice(start, end)}\nthis.completeWebChatReceiptUnderTest = completeWebChatReceipt;`,
    context,
  );
  return context.completeWebChatReceiptUnderTest;
}

test("renderer retries only the immutable Nexus receipt after transient failures", async () => {
  const calls = [];
  const outcomes = [
    Object.assign(new Error("offline"), {responseReceived: false}),
    Object.assign(new Error("temporarily unavailable"), {responseReceived: true, status: 503}),
    {accepted: true},
  ];
  const complete = receiptHelpers(async (url, options) => {
    calls.push({url, body: options.body});
    const outcome = outcomes.shift();
    if (outcome instanceof Error) throw outcome;
    return outcome;
  });

  const result = await complete(
    {request_id: "request-exact-17", route: "web:gemini-17"},
    {answer: "visible reply", delivery_state: "accepted"},
  );

  assert.equal(result.accepted, true);
  assert.equal(result.attempts, 3);
  assert.equal(calls.length, 3);
  assert.ok(calls.every((one) => one.url === "/api/web-chats/complete"));
  assert.ok(calls.every((one) => one.body === calls[0].body));
  assert.equal(JSON.parse(calls[0].body).request_id, "request-exact-17");
});

test("renderer treats an expired receipt as terminal and never retries it", async () => {
  const calls = [];
  const complete = receiptHelpers(async (url, options) => {
    calls.push({url, body: options.body});
    return {accepted: false, receipt_state: "expired_or_unknown"};
  });

  const result = await complete(
    {request_id: "expired-request", route: "web:claude-3"},
    {answer: "late visible reply"},
  );

  assert.equal(result.accepted, false);
  assert.equal(result.attempts, 1);
  assert.equal(calls.length, 1);
});

test("renderer does not retry a non-transient completion rejection", async () => {
  let calls = 0;
  const complete = receiptHelpers(async () => {
    calls += 1;
    throw Object.assign(new Error("bad receipt"), {responseReceived: true, status: 400});
  });

  const result = await complete(
    {request_id: "bad-request", route: "web:chatgpt-2"},
    {error: "provider stopped"},
  );

  assert.equal(result.accepted, null);
  assert.equal(result.attempts, 1);
  assert.equal(calls, 1);
});
