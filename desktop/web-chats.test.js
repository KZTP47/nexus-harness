"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {EventEmitter} = require("node:events");
const path = require("node:path");
const vm = require("node:vm");
const {
  PROVIDERS, WebChatManager, allowedProviderUrl, providerReadinessScript,
  composerTextSelectionScript,
  automationScript, submissionBaselineScript, submitControlScript, submissionScript,
  answerScript, retryScript, stopScript,
  browserLikeUserAgent, authStorageAccessIsTrusted,
} = require("./web-chats");

test("provider pages and explicit sign-in hosts stay inside the remote browser boundary", () => {
  assert.equal(allowedProviderUrl(PROVIDERS.claude, "https://claude.ai/new"), true);
  assert.equal(allowedProviderUrl(PROVIDERS.claude, "https://accounts.google.com/o/oauth2/v2/auth", true), true);
  assert.equal(allowedProviderUrl(PROVIDERS.claude, "https://appleid.apple.com/auth/authorize", true), true);
  assert.equal(allowedProviderUrl(PROVIDERS.claude, "http://claude.ai/new"), false);
  assert.equal(allowedProviderUrl(PROVIDERS.claude, "https://claude.ai.attacker.example/"), false);
  const localFile = "file:" + "///" + ["C:", "Windows", "System32"].join("/");
  assert.equal(allowedProviderUrl(PROVIDERS.claude, localFile), false);
});

test("Claude authorization identity and provider-declared challenge storage stay narrow", () => {
  assert.equal(browserLikeUserAgent(
    "Mozilla/5.0 our-harness-desktop/0.1.0 Chrome/150.0 Electron/43.4.1 Safari/537.36"
  ), "Mozilla/5.0 Chrome/150.0 Safari/537.36");
  assert.equal(authStorageAccessIsTrusted(
    PROVIDERS.claude, "storage-access", ["https://newassets.hcaptcha.com/captcha/"]
  ), true);
  assert.equal(authStorageAccessIsTrusted(
    PROVIDERS.claude, "top-level-storage-access", ["https://accounts.hcaptcha.com/"]
  ), true);
  assert.equal(authStorageAccessIsTrusted(
    PROVIDERS.claude, "storage-access", ["https://challenges.cloudflare.com/turnstile/v0/"]
  ), true);
  assert.equal(authStorageAccessIsTrusted(
    PROVIDERS.claude, "storage-access", ["https://attacker.example/"]
  ), false);
  assert.equal(authStorageAccessIsTrusted(
    PROVIDERS.claude, "notifications", ["https://newassets.hcaptcha.com/"]
  ), false);
});

test("the Claude partition applies the authorization identity and permission boundary", () => {
  let userAgent = "Mozilla/5.0 our-harness-desktop/0.1.0 Chrome/150.0 Electron/43.4.1 Safari/537.36";
  let check = null;
  let request = null;
  const held = {
    getUserAgent: () => userAgent,
    setUserAgent: (value) => { userAgent = value; },
    setPermissionCheckHandler: (handler) => { check = handler; },
    setPermissionRequestHandler: (handler) => { request = handler; },
    on: () => {},
  };
  const manager = new WebChatManager({
    electron: {session: {fromPartition: () => held}}, owner: null,
    readSettings: () => ({}), writeSettings: () => {},
    shellPage: "file:///web-chat.html", shellPreload: "web-chat-shell-preload.js",
  });

  assert.equal(manager.sessionFor("claude"), held);
  assert.ok(!userAgent.includes("Electron/"));
  assert.ok(!userAgent.includes("our-harness-desktop/"));
  assert.equal(check(null, "storage-access", "https://newassets.hcaptcha.com", {
    embeddingOrigin: "https://claude.ai",
  }), true);
  assert.equal(check(null, "geolocation", "https://newassets.hcaptcha.com", {}), false);
  let granted = null;
  request(null, "storage-access", (value) => { granted = value; }, {
    requestingUrl: "https://newassets.hcaptcha.com/captcha/",
  });
  assert.equal(granted, true);
  request(null, "storage-access", (value) => { granted = value; }, {
    requestingUrl: "https://claude.ai/",
    requestingOrigin: "https://hcaptcha.com/",
    embeddingOrigin: "https://claude.ai/",
  });
  assert.equal(granted, true);
  request(null, "notifications", (value) => { granted = value; }, {
    requestingUrl: "https://claude.ai/",
  });
  assert.equal(granted, false);
});

test("Claude uses the provider-configured external browser transport", () => {
  const dataRoot = path.win32.join("C:" + path.win32.sep, "NexusData");
  let received = null;
  const contents = {isDestroyed: () => false};
  const manager = new WebChatManager({
    electron: {app: {getPath: () => dataRoot}}, owner: null,
    readSettings: () => ({}), writeSettings: () => {},
    shellPage: "file:///web-chat.html", shellPreload: "web-chat-shell-preload.js",
    externalBrowserFactory: (options) => {
      received = options;
      return {createContents: (url) => {
        assert.equal(url, "https://claude.ai/login");
        return contents;
      }};
    },
  });

  const view = manager.makeRemoteView("claude", "https://claude.ai/login");

  assert.equal(view.external, true);
  assert.equal(view.webContents, contents);
  assert.equal(received.provider, PROVIDERS.claude);
  assert.deepEqual(received.preferred, ["chrome", "edge"]);
  assert.equal(received.profilePath, path.win32.join(dataRoot, "external-web-chat", "claude"));
  assert.equal(manager.providers().find((one) => one.id === "claude").external, true);
  assert.equal(manager.providers().find((one) => one.id === "gemini").external, false);
});

test("the remote-page script contains fixed selectors and safely encoded prompt text", () => {
  const prompt = "hello `); require('node:fs').rmSync('x'); //";
  const script = automationScript(PROVIDERS.chatgpt, prompt);
  assert.ok(script.includes(JSON.stringify(prompt)));
  assert.ok(script.includes("#prompt-textarea"));
  assert.ok(script.includes("#mobile-composer-prompt"));
  assert.ok(script.includes("data-user-message-bubble"));
  assert.ok(PROVIDERS.chatgpt.stop.includes("button[data-composer-submit][aria-label*='Stop']"));
  assert.ok(!PROVIDERS.chatgpt.stop.includes("button[data-composer-submit][data-stop-label]"));
  assert.ok(script.includes("send?.querySelector"));
  assert.ok(automationScript(PROVIDERS.gemini, prompt).includes('send?.tagName?.includes("-")'));
  assert.ok(script.includes("provider did not accept Send"));
  assert.ok(script.includes("for (const selector of list)"));
  const geminiSubmission = automationScript(PROVIDERS.gemini, prompt);
  assert.ok(geminiSubmission.includes('submissionState: "outcome_unknown"'));
  assert.ok(!geminiSubmission.includes("composerAccepted"));
  assert.ok(!script.includes("ipcRenderer"));
  const answer = answerScript(PROVIDERS.gemini, {beforeCount: 0, beforeLast: ""});
  assert.ok(answer.includes("model-response-text"));
  assert.ok(answer.includes("values.slice(began.beforeCount)"));
  assert.ok(answer.includes("userAdvanced && replyAdvanced"));
  for (const provider of Object.values(PROVIDERS)) {
    assert.equal(provider.pairRepliesToUsers, true, `${provider.label} must correlate replies to its exact user turn`);
  }
  assert.ok(!answer.includes("flatMap"));
  assert.ok(submissionScript(PROVIDERS.copilot, prompt, {}).includes("beforeUserCount"));
  assert.ok(retryScript(PROVIDERS.chatgpt).includes("data-conversation-recovery-retry"));
  assert.ok(providerReadinessScript(PROVIDERS.claude).includes("secure Nexus browser window"));
  assert.ok(composerTextSelectionScript(PROVIDERS.chatgpt).includes("selectNodeContents"));
  assert.ok(submissionBaselineScript(PROVIDERS.chatgpt, prompt).includes("needsTrustedInput"));
  assert.ok(submitControlScript(PROVIDERS.chatgpt, prompt).includes("elementsFromPoint"));
  assert.ok(submitControlScript(PROVIDERS.chatgpt, prompt).includes("scope.parentElement"));
  assert.equal(PROVIDERS.chatgpt.trustedInput, true);
  assert.equal(PROVIDERS.claude.trustedInput, true);
  assert.equal(PROVIDERS.gemini.trustedInput, true);
  assert.equal(PROVIDERS.copilot.trustedInput, true);
});

test("Claude's current streaming transcript boundary yields the completed marked-turn reply", () => {
  const element = (text) => ({
    innerText: text, textContent: text, getClientRects: () => [1],
    getAttribute: () => "",
  });
  const user = element("[NEXUS TRANSPORT TURN current-claude] who is this?");
  const answer = element("CLAUDE_CURRENT_TRANSCRIPT_OK");
  user.compareDocumentPosition = (node) => node === answer ? 4 : 2;
  const began = {
    beforeCount: 0, beforeLast: "", beforeUserCount: 0, beforeUserLast: "",
    beforeError: "",
    submittedPrompt: user.innerText,
    submittedMarker: "NEXUS TRANSPORT TURN current-claude",
  };
  const context = {
    document: {querySelectorAll: (selector) => {
      if (selector === "[data-is-streaming] .standard-markdown") return [answer];
      if (PROVIDERS.claude.users.includes(selector)) return [user];
      return [];
    }},
    getComputedStyle: () => ({visibility: "visible"}),
  };

  const state = vm.runInNewContext(answerScript(PROVIDERS.claude, began), context);

  assert.equal(PROVIDERS.claude.replies[0], "[data-is-streaming] .standard-markdown");
  assert.equal(state.changed, true);
  assert.equal(state.answer, "CLAUDE_CURRENT_TRANSCRIPT_OK");
});

test("one Send button matching exact and fallback selectors remains one safe target", () => {
  const visible = {
    getBoundingClientRect: () => ({left: 20, top: 30, width: 40, height: 40}),
  };
  const send = {
    ...visible, tagName: "BUTTON", id: "composer-submit-button", className: "composer-submit-btn",
    disabled: false, hasAttribute: () => false,
    getAttribute: (name) => ({
      "data-testid": "send-button", "aria-label": "Send prompt", role: "button",
    })[name] || null,
    matches: () => true, contains: (one) => one === send,
  };
  const scope = {
    querySelectorAll: (selector) => PROVIDERS.chatgpt.send.includes(selector) ? [send] : [],
    parentElement: null,
  };
  const composer = {
    ...visible, tagName: "DIV", innerText: "a long Nexus collaboration prompt",
    textContent: "a long Nexus collaboration prompt", className: "ProseMirror",
    querySelectorAll: () => [], parentElement: scope,
  };
  const context = {
    document: {
      querySelectorAll: (selector) => PROVIDERS.chatgpt.composer.includes(selector)
        ? [composer] : [],
      elementsFromPoint: () => [send],
    },
    getComputedStyle: () => ({visibility: "visible", display: "block"}),
    HTMLTextAreaElement: class {}, HTMLInputElement: class {},
    Set,
  };

  const state = vm.runInNewContext(submitControlScript(
    PROVIDERS.chatgpt, composer.innerText), context);

  assert.equal(state.ready, true);
  assert.equal(state.code, "ready");
  assert.equal(state.fingerprint.includes("send-button"), true);
});

test("Gemini pairs a reformatted long Nexus prompt with the reply after it", () => {
  const element = (text) => ({
    innerText: text, textContent: text, getClientRects: () => [1],
    getAttribute: () => "",
  });
  const replies = [element("older reply"), element("GEMINI_LONG_CONTEXT_OK")];
  const users = [
    element("older request"),
    element("NEXUS WEB-CHAT TURN You are participating as an AI agent Task from Nexus: reply now"),
  ];
  const began = {
    beforeCount: 1, beforeLast: "older reply",
    beforeUserCount: 1, beforeUserLast: "older request", beforeError: "",
    submittedPrompt: "NEXUS WEB-CHAT TURN\n\nYou are participating as an AI agent\n\nTask from Nexus: reply now",
  };
  const context = {
    document: {querySelectorAll: (selector) => {
      if (PROVIDERS.gemini.replies.includes(selector)) return replies;
      if (PROVIDERS.gemini.users.includes(selector)) return users;
      return [];
    }},
    getComputedStyle: () => ({visibility: "visible"}),
  };

  const state = vm.runInNewContext(answerScript(PROVIDERS.gemini, began), context);

  assert.equal(state.changed, true);
  assert.equal(state.answer, "GEMINI_LONG_CONTEXT_OK");
});

test("Gemini rejects a stale routing reply after a new prompt until a new reply exists", () => {
  const element = (text) => ({
    innerText: text, textContent: text, getClientRects: () => [1],
    getAttribute: () => "",
  });
  const began = {
    beforeCount: 1, beforeLast: '{"collaborate":false,"reason":"direct"}',
    beforeUserCount: 1, beforeUserLast: "old router prompt", beforeError: "",
    submittedPrompt: "Task from Nexus: Gemini, is this you?",
  };
  const context = {
    document: {querySelectorAll: (selector) => {
      if (PROVIDERS.gemini.replies.includes(selector)) return [element(began.beforeLast)];
      if (PROVIDERS.gemini.users.includes(selector)) return [
        element("old router prompt"), element("Task from Nexus: Gemini, is this you?"),
      ];
      return [];
    }},
    getComputedStyle: () => ({visibility: "visible"}),
  };

  const state = vm.runInNewContext(answerScript(PROVIDERS.gemini, began), context);

  assert.equal(state.changed, false);
  assert.equal(state.answer, "");
});

test("Gemini rejects a remounted old turn when the baseline was temporarily empty", () => {
  const element = (text) => ({
    innerText: text, textContent: text, getClientRects: () => [1],
    getAttribute: () => "",
  });
  const began = {
    beforeCount: 0, beforeLast: "", beforeUserCount: 0, beforeUserLast: "",
    beforeError: "", submittedPrompt: "Team plan review round 2: mark readiness",
  };
  const context = {
    document: {querySelectorAll: (selector) => {
      if (PROVIDERS.gemini.replies.includes(selector)) return [
        element('{"contribution":"old initial plan","needs_files":[]}'),
      ];
      if (PROVIDERS.gemini.users.includes(selector)) return [
        element("Initial planning request for 02020202"),
      ];
      return [];
    }},
    getComputedStyle: () => ({visibility: "visible"}),
  };

  const state = vm.runInNewContext(answerScript(PROVIDERS.gemini, began), context);

  assert.equal(state.changed, false);
  assert.equal(state.answer, '{"contribution":"old initial plan","needs_files":[]}');
});

test("ChatGPT rejects an old remounted task even when the page baseline was empty", () => {
  const element = (text) => ({
    innerText: text, textContent: text, getClientRects: () => [1],
    getAttribute: () => "",
  });
  const began = {
    beforeCount: 0, beforeLast: "", beforeUserCount: 0, beforeUserLast: "",
    beforeError: "", submittedPrompt: "[NEXUS TRANSPORT TURN fresh-task]\n\nnew snake task",
    submittedMarker: "NEXUS TRANSPORT TURN fresh-task",
  };
  const context = {
    document: {querySelectorAll: (selector) => {
      if (PROVIDERS.chatgpt.replies.includes(selector)) return [
        element('{"contribution":"old identity answer"}'),
      ];
      if (PROVIDERS.chatgpt.users.includes(selector)) return [
        element("[NEXUS TRANSPORT TURN old-task] old identity request"),
      ];
      return [];
    }},
    getComputedStyle: () => ({visibility: "visible"}),
  };

  const stale = vm.runInNewContext(answerScript(PROVIDERS.chatgpt, began), context);
  assert.equal(stale.changed, false);

  context.document.querySelectorAll = (selector) => {
    if (PROVIDERS.chatgpt.replies.includes(selector)) return [
      element('{"contribution":"new snake plan"}'),
    ];
    if (PROVIDERS.chatgpt.users.includes(selector)) return [
      element("[NEXUS TRANSPORT TURN fresh-task] new snake task"),
    ];
    return [];
  };
  const fresh = vm.runInNewContext(answerScript(PROVIDERS.chatgpt, began), context);
  assert.equal(fresh.changed, true);
  assert.equal(fresh.answer, '{"contribution":"new snake plan"}');
});

test("Gemini waits for a reply node after the uniquely marked new user turn", () => {
  const oldReply = {
    innerText: "GEMINI_OLD_TASK", textContent: "GEMINI_OLD_TASK",
    getClientRects: () => [1], getAttribute: () => "",
  };
  let freshReply = null;
  const freshUser = {
    innerText: "[NEXUS TRANSPORT TURN fresh-order] new task",
    textContent: "[NEXUS TRANSPORT TURN fresh-order] new task",
    getClientRects: () => [1], getAttribute: () => "",
    compareDocumentPosition: (node) => node === freshReply ? 4 : 2,
  };
  const began = {
    beforeCount: 0, beforeLast: "", beforeUserCount: 0, beforeUserLast: "",
    beforeError: "", submittedPrompt: freshUser.innerText,
    submittedMarker: "NEXUS TRANSPORT TURN fresh-order",
  };
  const replies = [oldReply];
  const context = {
    document: {querySelectorAll: (selector) => {
      if (PROVIDERS.gemini.replies.includes(selector)) return replies;
      if (PROVIDERS.gemini.users.includes(selector)) return [freshUser];
      return [];
    }},
    getComputedStyle: () => ({visibility: "visible"}),
  };

  const waiting = vm.runInNewContext(answerScript(PROVIDERS.gemini, began), context);
  assert.equal(waiting.changed, false);
  assert.equal(waiting.answer, "");

  freshReply = {
    innerText: "GEMINI_NEW_TASK", textContent: "GEMINI_NEW_TASK",
    getClientRects: () => [1], getAttribute: () => "",
  };
  replies.push(freshReply);
  const complete = vm.runInNewContext(answerScript(PROVIDERS.gemini, began), context);
  assert.equal(complete.changed, true);
  assert.equal(complete.answer, "GEMINI_NEW_TASK");
});

test("Gemini trusted Enter confirmation requires the exact new user turn", async () => {
  const element = (text) => ({
    innerText: text, textContent: text, getClientRects: () => [1],
    getBoundingClientRect: () => ({width: 10, height: 10}),
  });
  const began = {
    ok: false, needsTrustedEnter: true,
    beforeCount: 0, beforeLast: "", beforeUserCount: 0, beforeUserLast: "",
  };
  const context = {
    document: {querySelectorAll: (selector) => {
      if (PROVIDERS.gemini.replies.includes(selector)) return [element("old reply")];
      if (PROVIDERS.gemini.users.includes(selector)) return [element("old initial request")];
      if (PROVIDERS.gemini.stop.includes(selector)) return [element("")];
      return [];
    }},
    getComputedStyle: () => ({visibility: "visible", display: "block"}),
    HTMLTextAreaElement: class {}, HTMLInputElement: class {},
    setTimeout: (callback) => { callback(); return 1; },
  };

  const state = await vm.runInNewContext(submissionScript(
    PROVIDERS.gemini, "new plan review prompt", began
  ), context);

  assert.equal(state.ok, false);
  assert.equal(state.needsTrustedEnter, true);
});

test("Gemini treats a clicked turn with only a stale Stop control as outcome-unknown", async () => {
  let sendClicks = 0;
  const visible = {
    getBoundingClientRect: () => ({width: 10, height: 10}),
    getClientRects: () => [1],
  };
  const composer = {
    ...visible, textContent: "", focus: () => {},
    replaceChildren(node) { this.textContent = node.text; }, dispatchEvent: () => true,
  };
  const send = {
    ...visible, matches: () => true, disabled: false, hasAttribute: () => false,
    getAttribute: () => null, click: () => { sendClicks += 1; },
  };
  const stop = {...visible};
  const oldUser = {...visible, innerText: "old router prompt", textContent: "old router prompt"};
  const oldReply = {
    ...visible, innerText: '{"collaborate":false,"reason":"direct"}',
    textContent: '{"collaborate":false,"reason":"direct"}', getAttribute: () => "",
  };
  const context = {
    document: {
      createTextNode: (text) => ({text}),
      querySelectorAll: (selector) => {
        if (PROVIDERS.gemini.composer.includes(selector)) return [composer];
        if (PROVIDERS.gemini.send.includes(selector)) return [send];
        if (PROVIDERS.gemini.stop.includes(selector)) return [stop];
        if (PROVIDERS.gemini.users.includes(selector)) return [oldUser];
        if (PROVIDERS.gemini.replies.includes(selector)) return [oldReply];
        return [];
      },
    },
    getComputedStyle: () => ({visibility: "visible", display: "block"}),
    location: {pathname: "/app/example"},
    HTMLTextAreaElement: class {}, HTMLInputElement: class {},
    InputEvent: class {}, Event: class {}, KeyboardEvent: class {},
    setTimeout: (callback) => { callback(); return 1; },
  };

  const result = await vm.runInNewContext(
    automationScript(PROVIDERS.gemini, "new direct prompt"), context);

  assert.equal(sendClicks, 1);
  assert.equal(result.ok, true);
  assert.equal(result.needsTrustedEnter, false);
  assert.equal(result.submissionState, "outcome_unknown");
  assert.equal(result.beforeStopping, true);
});

test("Gemini composer clearing alone remains outcome-unknown", async () => {
  const visible = {
    getBoundingClientRect: () => ({width: 10, height: 10}),
    getClientRects: () => [1],
  };
  const composer = {
    ...visible, textContent: "", focus: () => {},
    replaceChildren(node) { this.textContent = node.text; }, dispatchEvent: () => true,
  };
  let hostClicks = 0;
  const send = {
    ...visible, tagName: "GEM-ICON-BUTTON", matches: () => false,
    disabled: false, hasAttribute: () => false, getAttribute: () => "false",
    click: () => { hostClicks += 1; composer.textContent = ""; },
    querySelector: () => ({click: () => { throw new Error("inner button must not be clicked"); }}),
  };
  const context = {
    document: {
      createTextNode: (text) => ({text}),
      querySelectorAll: (selector) => {
        if (PROVIDERS.gemini.composer.includes(selector)) return [composer];
        if (PROVIDERS.gemini.send.includes(selector)) return [send];
        return [];
      },
    },
    getComputedStyle: () => ({visibility: "visible", display: "block"}),
    location: {pathname: "/app"},
    HTMLTextAreaElement: class {}, HTMLInputElement: class {},
    InputEvent: class {}, Event: class {}, KeyboardEvent: class {},
    setTimeout: (callback) => { callback(); return 1; },
  };

  const result = await vm.runInNewContext(
    automationScript(PROVIDERS.gemini, "new directed relay"), context);

  assert.equal(hostClicks, 1);
  assert.equal(result.ok, true);
  assert.equal(result.sendActivated, true);
  assert.equal(result.submissionState, "outcome_unknown");
});

test("ChatGPT composer clearing alone remains outcome-unknown", async () => {
  const visible = {
    getBoundingClientRect: () => ({width: 10, height: 10}),
    getClientRects: () => [1],
  };
  const composer = {
    ...visible, textContent: "", focus: () => {},
    replaceChildren(node) { this.textContent = node.text; }, dispatchEvent: () => true,
  };
  const send = {
    ...visible, tagName: "BUTTON", matches: () => true, disabled: false,
    hasAttribute: () => false, getAttribute: () => null,
    click: () => { composer.textContent = ""; },
  };
  const context = {
    document: {
      createTextNode: (text) => ({text}),
      querySelectorAll: (selector) => {
        if (PROVIDERS.chatgpt.composer.includes(selector)) return [composer];
        if (PROVIDERS.chatgpt.send.includes(selector)) return [send];
        return [];
      },
    },
    getComputedStyle: () => ({visibility: "visible", display: "block"}),
    location: {pathname: "/c/example"},
    HTMLTextAreaElement: class {}, HTMLInputElement: class {},
    InputEvent: class {}, Event: class {}, KeyboardEvent: class {},
    setTimeout: (callback) => { callback(); return 1; },
  };

  const result = await vm.runInNewContext(automationScript(
    PROVIDERS.chatgpt,
    "[NEXUS TRANSPORT TURN fresh-chatgpt] new task",
    "NEXUS TRANSPORT TURN fresh-chatgpt",
  ), context);

  assert.equal(result.ok, true);
  assert.equal(result.sendActivated, true);
  assert.equal(result.submissionState, "outcome_unknown");
  assert.equal(result.submittedMarker, "NEXUS TRANSPORT TURN fresh-chatgpt");
  assert.equal(PROVIDERS.chatgpt.pairRepliesToUsers, true);
});

test("using the current chat identifies it to the board as the selected connection", async () => {
  const sent = [];
  let settings = {};
  const manager = new WebChatManager({
    electron: {},
    owner: {
      isDestroyed: () => false,
      webContents: {send: (...args) => sent.push(args)},
    },
    readSettings: () => settings,
    writeSettings: (value) => { settings = value; },
    shellPage: "file:///web-chat.html",
    shellPreload: "web-chat-shell-preload.js",
  });
  manager.shellFor = () => ({
    providerId: "chatgpt",
    connectionId: "",
    view: {webContents: {
      isLoading: () => false,
      executeJavaScript: async () => ({ready: true}),
      getURL: () => "https://chatgpt.com/c/example",
      getTitle: async () => "Release helper",
    }},
  });

  const selected = await manager.useCurrent({});

  assert.equal(selected.title, "Release helper");
  assert.equal(settings.webChats.length, 1);
  assert.equal(sent.length, 1);
  assert.equal(sent[0][0], "harness:webChatsChanged");
  assert.deepEqual(sent[0][1], [selected]);
  assert.deepEqual(sent[0][2], selected);
});

test("using Claude current chat adopts the selected replacement browser tab first", async () => {
  let settings = {};
  let adopted = 0;
  const contents = {
    useCurrentPage: async () => { adopted += 1; },
    isLoading: () => false,
    executeJavaScript: async () => ({ready: true}),
    getURL: () => "https://claude.ai/chat/selected-after-oauth",
    getTitle: async () => "Selected Claude chat",
  };
  const manager = new WebChatManager({
    electron: {}, owner: null,
    readSettings: () => settings,
    writeSettings: (value) => { settings = value; },
    shellPage: "file:///web-chat.html", shellPreload: "web-chat-shell-preload.js",
  });
  manager.shellFor = () => ({
    providerId: "claude", connectionId: "", view: {webContents: contents},
  });

  const selected = await manager.useCurrent({});

  assert.equal(adopted, 1);
  assert.equal(selected.provider, "claude");
  assert.equal(selected.url, "https://claude.ai/chat/selected-after-oauth");
});

test("a login page cannot be saved as if it were a connected AI chat", async () => {
  let settings = {};
  const manager = new WebChatManager({
    electron: {}, owner: null,
    readSettings: () => settings,
    writeSettings: (value) => { settings = value; },
    shellPage: "file:///web-chat.html",
    shellPreload: "web-chat-shell-preload.js",
  });
  manager.shellFor = () => ({
    providerId: "claude", connectionId: "",
    view: {webContents: {
      isLoading: () => false,
      getURL: () => "https://claude.ai/login",
      executeJavaScript: async () => ({
        ready: false,
        reason: "Sign in to Claude in the secure Nexus browser window first.",
      }),
    }},
  });

  await assert.rejects(() => manager.useCurrent({}), /Sign in to Claude in the secure Nexus browser window/);
  assert.deepEqual(settings, {});
});

test("a provider SPA navigation replaces the generic saved page with the real conversation", () => {
  const sent = [];
  let settings = {webChats: [{
    id: "gemini-example", provider: "gemini", title: "Google Gemini",
    url: "https://gemini.google.com/app",
  }]};
  const manager = new WebChatManager({
    electron: {},
    owner: {
      isDestroyed: () => false,
      webContents: {send: (...args) => sent.push(args)},
    },
    readSettings: () => settings,
    writeSettings: (value) => { settings = value; },
    shellPage: "file:///web-chat.html",
    shellPreload: "web-chat-shell-preload.js",
  });
  const contents = Object.assign(new EventEmitter(), {
    url: "https://gemini.google.com/app",
    title: "Google Gemini",
    getURL() { return this.url; },
    getTitle() { return this.title; },
    isDestroyed: () => false,
  });
  manager.trackConnectionPage("gemini-example", contents);

  contents.url = "https://gemini.google.com/app/1234567890abcdef";
  contents.title = "Nexus release notes - Gemini";
  contents.emit("did-navigate-in-page");

  assert.equal(settings.webChats[0].url, contents.url);
  assert.equal(settings.webChats[0].title, contents.title);
  assert.equal(sent.at(-1)[0], "harness:webChatsChanged");
});

test("a newly created conversation replaces the stale generic page in an open provider window", async () => {
  const manager = new WebChatManager({
    electron: {}, owner: null,
    readSettings: () => ({}), writeSettings: () => {},
    shellPage: "file:///web-chat.html",
    shellPreload: "web-chat-shell-preload.js",
  });
  const priorUrl = "https://gemini.google.com/app";
  const conversationUrl = "https://gemini.google.com/app/1234567890abcdef";
  manager.connections.set("gemini-example", {
    id: "gemini-example", provider: "gemini", title: "A real chat", url: conversationUrl,
  });
  const loaded = [];
  const shellContents = {
    getURL: () => priorUrl, isDestroyed: () => false,
    loadURL: async (url) => { loaded.push(url); },
  };
  manager.shells.set(1, {
    connectionId: "gemini-example", conversationKey: "",
    view: {webContents: shellContents},
  });

  manager.showCreatedConversationInOpenShells(
    "gemini-example", "", {}, priorUrl);
  await Promise.resolve();

  assert.deepEqual(loaded, [conversationUrl]);
});

test("every Nexus chat gets a distinct provider thread for every web provider", () => {
  const originals = {
    chatgpt: "https://chatgpt.com/c/original-thread",
    claude: "https://claude.ai/chat/original-thread",
    gemini: "https://gemini.google.com/app/original-thread",
    copilot: "https://copilot.microsoft.com/chats/original-thread",
  };
  for (const [providerId, originalUrl] of Object.entries(originals)) {
    let settings = {};
    const manager = new WebChatManager({
      electron: {}, owner: null,
      readSettings: () => settings,
      writeSettings: (value) => { settings = value; },
      shellPage: "file:///web-chat.html",
      shellPreload: "web-chat-shell-preload.js",
    });
    const connectionId = `${providerId}-example`;
    manager.connections.set(connectionId, {
      id: connectionId, provider: providerId,
      title: `${providerId} original`, url: originalUrl, threads: {},
    });
    const made = [];
    manager.makeRemoteView = () => {
      const contents = Object.assign(new EventEmitter(), {
        url: "", title: "",
        isDestroyed: () => false,
        getURL() { return this.url; },
        getTitle() { return this.title; },
        loadURL(url) { this.url = url; return Promise.resolve(); },
      });
      const view = {webContents: contents};
      made.push(view);
      return view;
    };
    manager.parkBackgroundView = () => {};

    const first = manager.viewFor(connectionId, "pair-chat-one", true);
    const second = manager.viewFor(connectionId, "pair-chat-two", false);

    assert.notEqual(first, second, `${providerId} reused one WebContentsView`);
    assert.equal(first.webContents.getURL(), originalUrl);
    assert.equal(second.webContents.getURL(), PROVIDERS[providerId].newChat);
    assert.equal(made.length, 2);

    const secondUrl = originals[providerId].replace("original-thread", "second-thread");
    second.webContents.url = secondUrl;
    second.webContents.title = `${providerId} second`;
    assert.equal(manager.rememberConnectionPage(
      connectionId, second.webContents, "pair-chat-two"), true);
    const saved = settings.webChats[0];
    assert.equal(saved.threads["pair-chat-one"].url, originalUrl);
    assert.equal(saved.threads["pair-chat-two"].url, secondUrl);
  }
});

test("starting a Nexus chat again discards only that chat's remote binding", () => {
  let settings = {};
  let closed = 0;
  const manager = new WebChatManager({
    electron: {}, owner: null,
    readSettings: () => settings,
    writeSettings: (value) => { settings = value; },
    shellPage: "file:///web-chat.html", shellPreload: "web-chat-shell-preload.js",
  });
  manager.connections.set("gemini-example", {
    id: "gemini-example", provider: "gemini", title: "Gemini",
    url: "https://gemini.google.com/app/original", threads: {
      "pair-chat-one": {url: "https://gemini.google.com/app/one", title: "One"},
      "pair-chat-two": {url: "https://gemini.google.com/app/two", title: "Two"},
    },
  });
  manager.views.set("gemini-example\npair-chat-two", {webContents: {
    isDestroyed: () => false, close: () => { closed += 1; },
  }});

  assert.equal(manager.resetThread("gemini-example", "pair-chat-two"), true);

  assert.equal(closed, 1);
  assert.ok(settings.webChats[0].threads["pair-chat-one"]);
  assert.equal(settings.webChats[0].threads["pair-chat-two"], undefined);
  assert.equal(manager.views.has("gemini-example\npair-chat-two"), false);
});

test("a background provider view gets a real hidden rendering host", () => {
  const bounds = [];
  const attached = [];
  const detached = [];
  let createdOptions = null;
  let hostOptions = null;
  let closed = 0;
  class FakeView {
    constructor(options) {
      createdOptions = options;
      this.webContents = {
        setWindowOpenHandler: () => {},
        on: () => {},
      };
    }
    setBounds(value) { bounds.push(value); }
  }
  class FakeWindow {
    constructor(options) {
      hostOptions = options;
      this.contentView = {
        addChildView: (view) => attached.push(view),
        removeChildView: (view) => detached.push(view),
      };
    }
    isDestroyed() { return false; }
    on() {}
    close() { closed += 1; }
  }
  const manager = new WebChatManager({
    electron: {
      WebContentsView: FakeView, BrowserWindow: FakeWindow,
      session: {fromPartition: () => ({
        __nexusWebChatGuarded: true,
      })},
    },
    owner: {
      isDestroyed: () => false,
      contentView: {addChildView: () => {}, removeChildView: () => {}},
    },
    readSettings: () => ({}), writeSettings: () => {},
    shellPage: "file:///web-chat.html",
    shellPreload: "web-chat-shell-preload.js",
  });

  const view = manager.makeRemoteView("gemini");
  manager.parkBackgroundView(view);

  assert.deepEqual(attached, [view]);
  assert.equal(hostOptions.show, false);
  assert.equal(hostOptions.skipTaskbar, true);
  assert.equal(hostOptions.webPreferences.backgroundThrottling, false);
  assert.equal(createdOptions.webPreferences.backgroundThrottling, false);
  assert.deepEqual(bounds, [
    {x: 0, y: 0, width: 1200, height: 900},
    {x: 0, y: 0, width: 1200, height: 900},
  ]);
  manager.releaseBackgroundHost(view);
  assert.deepEqual(detached, [view]);
  assert.equal(closed, 1);
});

test("the provider load wait cannot miss a just-finished load", async () => {
  const contents = new EventEmitter();
  let checks = 0;
  contents.isLoading = () => ++checks === 1;
  const manager = new WebChatManager({
    electron: {}, owner: null,
    readSettings: () => ({}), writeSettings: () => {},
    shellPage: "file:///web-chat.html",
    shellPreload: "web-chat-shell-preload.js",
  });

  await manager.waitForLoad(contents);
  assert.equal(checks, 2);
});

test("an external provider waits for its hydrated composer before submission", async () => {
  let checks = 0;
  const manager = new WebChatManager({
    electron: {}, owner: null,
    readSettings: () => ({}), writeSettings: () => {},
    shellPage: "file:///web-chat.html", shellPreload: "web-chat-shell-preload.js",
    providerReadyDeadlineMs: 1000, providerReadyPollMs: 1,
  });
  const contents = {
    executeJavaScript: async () => ({
      ready: ++checks >= 3, reason: "Claude is still mounting its editor",
    }),
  };

  const readiness = await manager.waitForProviderReady(contents, PROVIDERS.claude);

  assert.equal(readiness.ready, true);
  assert.equal(checks, 3);
});

test("stopping a web turn marks only that chat cancelled and clicks the provider stop control", async () => {
  const scripts = [];
  const manager = new WebChatManager({
    electron: {}, owner: null,
    readSettings: () => ({}), writeSettings: () => {},
    shellPage: "file:///web-chat.html",
    shellPreload: "web-chat-shell-preload.js",
  });
  manager.connections.set("gemini-example", {
    id: "gemini-example", provider: "gemini", title: "Gemini", url: PROVIDERS.gemini.home,
  });
  manager.views.set("gemini-example", {webContents: {
    isDestroyed: () => false,
    executeJavaScript: async (script) => { scripts.push(script); return true; },
  }});
  const active = {cancelled: false};
  manager.activeAsks.set("gemini-example", active);
  manager.activeAsks.set("other-chat", {cancelled: false});

  assert.equal(await manager.stop("gemini-example"), true);
  assert.equal(active.cancelled, true);
  assert.equal(manager.activeAsks.get("other-chat").cancelled, false);
  assert.ok(scripts[0].includes(".stop-button"));
  assert.ok(stopScript(PROVIDERS.chatgpt).includes("stop-button"));
});

test("an accepted Gemini turn with no reply progress is never resubmitted", async () => {
  let submissions = 0;
  let stops = 0;
  const transportMarkers = [];
  const contents = {
    isDestroyed: () => false,
    executeJavaScript: async (script) => {
      if (script.includes("const prompt =")) {
        submissions += 1;
        transportMarkers.push(script.match(/NEXUS TRANSPORT TURN [0-9a-f-]{36}/)?.[0] || "");
        return {ok: true, beforeCount: 0, beforeLast: ""};
      }
      if (script.includes("const began =")) {
        return {answer: "", changed: false, stopping: true};
      }
      stops += 1;
      return true;
    },
  };
  const manager = new WebChatManager({
    electron: {}, owner: null,
    readSettings: () => ({}), writeSettings: () => {},
    shellPage: "file:///web-chat.html", shellPreload: "web-chat-shell-preload.js",
    answerPollMs: 2, answerDeadlineMs: 1000,
  });
  manager.connections.set("gemini-example", {
    id: "gemini-example", provider: "gemini", title: "Gemini", url: PROVIDERS.gemini.home,
  });
  manager.viewFor = () => ({webContents: contents});
  manager.waitForLoad = async () => {};
  manager.attachFiles = async () => {};
  manager.rememberConnectionPage = () => false;
  manager.showCreatedConversationInOpenShells = () => {};

  await assert.rejects(
    () => manager.askNow("gemini-example", "Please answer", []),
    /did not finish a visible reply/,
  );
  assert.equal(submissions, 1);
  assert.ok(stops >= 1);
  assert.ok(transportMarkers.every(Boolean));
});

test("Gemini is never retried after any visible reply progress", async () => {
  let submissions = 0;
  let answerChecks = 0;
  const contents = {
    isDestroyed: () => false,
    executeJavaScript: async (script) => {
      if (script.includes("const prompt =")) {
        submissions += 1;
        return {ok: true, beforeCount: 0, beforeLast: ""};
      }
      answerChecks += 1;
      return answerChecks < 3
        ? {answer: "Partial", changed: true, stopping: true}
        : {answer: "Complete", changed: true, stopping: false};
    },
  };
  const manager = new WebChatManager({
    electron: {}, owner: null,
    readSettings: () => ({}), writeSettings: () => {},
    shellPage: "file:///web-chat.html", shellPreload: "web-chat-shell-preload.js",
    answerPollMs: 2, answerDeadlineMs: 1000,
  });
  manager.connections.set("gemini-example", {
    id: "gemini-example", provider: "gemini", title: "Gemini", url: PROVIDERS.gemini.home,
  });
  manager.viewFor = () => ({webContents: contents});
  manager.waitForLoad = async () => {};
  manager.attachFiles = async () => {};
  manager.rememberConnectionPage = () => false;
  manager.showCreatedConversationInOpenShells = () => {};

  const result = await manager.askNow("gemini-example", "Please answer", []);

  assert.equal(result.answer, "Complete");
  assert.equal(submissions, 1);
});

test("an activated turn with delayed acknowledgement becomes outcome-unknown without Enter", async () => {
  const began = {
    ok: false, sendActivated: true, needsTrustedEnter: true,
    beforeCount: 0, beforeLast: "", beforeUserCount: 0, beforeUserLast: "",
  };
  const context = {
    document: {querySelectorAll: () => []},
    getComputedStyle: () => ({visibility: "visible", display: "block"}),
    HTMLTextAreaElement: class {}, HTMLInputElement: class {},
    setTimeout: (callback) => { callback(); return 1; },
  };

  const state = await vm.runInNewContext(submissionScript(
    PROVIDERS.chatgpt, "marked prompt", began
  ), context);

  assert.equal(state.ok, true);
  assert.equal(state.needsTrustedEnter, false);
  assert.equal(state.submissionState, "outcome_unknown");
});

test("an activated pointer with the exact enabled draft proves not-accepted and permits one Enter", async () => {
  const visible = {
    getBoundingClientRect: () => ({width: 10, height: 10}), getClientRects: () => [1],
  };
  const composer = {
    ...visible, innerText: "marked long prompt", textContent: "marked long prompt",
  };
  const send = {
    ...visible, disabled: false, hasAttribute: () => false,
    getAttribute: () => "false",
  };
  const began = {
    ok: false, sendActivated: true, needsTrustedEnter: false,
    beforeCount: 0, beforeLast: "", beforeUserCount: 0, beforeUserLast: "",
  };
  const context = {
    document: {querySelectorAll: (selector) => {
      if (PROVIDERS.chatgpt.composer.includes(selector)) return [composer];
      if (PROVIDERS.chatgpt.send.includes(selector)) return [send];
      return [];
    }},
    getComputedStyle: () => ({visibility: "visible", display: "block"}),
    HTMLTextAreaElement: class {}, HTMLInputElement: class {},
    setTimeout: (callback) => { callback(); return 1; },
  };

  const state = await vm.runInNewContext(submissionScript(
    PROVIDERS.chatgpt, "marked long prompt", began), context);

  assert.equal(state.ok, false);
  assert.equal(state.needsTrustedEnter, true);
  assert.equal(state.submissionState, "not_accepted");
});

test("the embedded trusted Enter fallback focuses the composer and sends key text through CDP", async () => {
  let attached = false;
  const commands = [];
  const manager = new WebChatManager({
    electron: {}, owner: null,
    readSettings: () => ({}), writeSettings: () => {},
    shellPage: "file:///web-chat.html", shellPreload: "web-chat-shell-preload.js",
  });
  const contents = {
    debugger: {
      isAttached: () => attached, attach: () => { attached = true; },
      detach: () => { attached = false; },
      sendCommand: async (method, parameters) => commands.push([method, parameters]),
    },
    executeJavaScript: async (script) => {
      assert.ok(script.includes("document.activeElement === composer"));
      return true;
    },
  };

  assert.equal(await manager.pressTrustedEnter(contents, PROVIDERS.chatgpt), true);
  assert.equal(attached, false);
  assert.deepEqual(commands.map(([method]) => method), [
    "Emulation.setFocusEmulationEnabled", "Input.dispatchKeyEvent", "Input.dispatchKeyEvent",
  ]);
  assert.equal(commands[1][1].type, "keyDown");
  assert.equal(commands[1][1].text, "\r");
  assert.equal(commands[2][1].type, "keyUp");
});

test("a stable partial Gemini reply is not committed while its Stop control stays stuck", async () => {
  let stoppedRemote = false;
  let stops = 0;
  const contents = {
    isDestroyed: () => false,
    executeJavaScript: async (script) => {
      if (script.includes("const prompt =")) {
        return {ok: true, beforeCount: 0, beforeLast: "", beforeError: ""};
      }
      if (script.includes("const began =")) {
        return {
          answer: "Complete but still marked streaming", changed: true,
          stopping: !stoppedRemote, error: "",
        };
      }
      stops += 1;
      stoppedRemote = true;
      return true;
    },
  };
  const manager = new WebChatManager({
    electron: {}, owner: null,
    readSettings: () => ({}), writeSettings: () => {},
    shellPage: "file:///web-chat.html", shellPreload: "web-chat-shell-preload.js",
    answerPollMs: 2, answerDeadlineMs: 1000,
  });
  manager.connections.set("gemini-example", {
    id: "gemini-example", provider: "gemini", title: "Gemini", url: PROVIDERS.gemini.home,
  });
  manager.viewFor = () => ({webContents: contents});
  manager.waitForLoad = async () => {};
  manager.attachFiles = async () => {};
  manager.rememberConnectionPage = () => false;
  manager.showCreatedConversationInOpenShells = () => {};

  await assert.rejects(
    () => manager.askNow("gemini-example", "Please answer", []),
    /did not finish a visible reply/,
  );
  assert.equal(stops, 1);
});

test("a provider with no clickable send control gets one trusted Enter fallback", async () => {
  const inputEvents = [];
  let answerChecks = 0;
  const focusOrder = [];
  const baseline = {
    beforeCount: 0, beforeLast: "", beforeUserCount: 0,
    beforeUserLast: "", beforeError: "",
  };
  const contents = {
    isDestroyed: () => false,
    focus: () => focusOrder.push("provider"),
    sendInputEvent: (event) => inputEvents.push(event),
    executeJavaScript: async (script) => {
      if (script.includes("const before = values(selectors.replies)")) {
        return {ok: false, needsTrustedEnter: true, error: "not sent", ...baseline};
      }
      if (script.includes("needsTrustedEnter: false")) {
        return {ok: true, needsTrustedEnter: false, error: "", ...baseline};
      }
      answerChecks += 1;
      return {answer: "Trusted reply", changed: true, stopping: false, error: ""};
    },
  };
  const manager = new WebChatManager({
    electron: {}, owner: {
      isDestroyed: () => false,
      webContents: {focus: () => focusOrder.push("board")},
    },
    readSettings: () => ({}), writeSettings: () => {},
    shellPage: "file:///web-chat.html", shellPreload: "web-chat-shell-preload.js",
    answerPollMs: 2, answerDeadlineMs: 1000,
  });
  manager.connections.set("copilot-example", {
    id: "copilot-example", provider: "copilot", title: "Copilot", url: PROVIDERS.copilot.home,
  });
  manager.viewFor = () => ({webContents: contents});
  manager.waitForLoad = async () => {};
  manager.attachFiles = async () => {};
  manager.rememberConnectionPage = () => false;
  manager.showCreatedConversationInOpenShells = () => {};

  const result = await manager.askNow("copilot-example", "Please answer", []);

  assert.equal(result.answer, "Trusted reply");
  assert.deepEqual(inputEvents, [
    {type: "keyDown", keyCode: "ENTER"},
    {type: "keyUp", keyCode: "ENTER"},
  ]);
  assert.ok(answerChecks >= 3);
  assert.deepEqual(focusOrder, ["provider", "board"]);
});

test("ChatGPT submission uses native text input and a trusted pointer before polling", async () => {
  const commands = [];
  const scripts = [];
  let attached = false;
  let answerChecks = 0;
  const baseline = {
    beforeCount: 0, beforeLast: "", beforeUserCount: 0,
    beforeUserLast: "", beforeError: "", beforeStopping: false,
  };
  const contents = {
    isDestroyed: () => false,
    focus: () => {},
    debugger: {
      isAttached: () => attached,
      attach: () => { attached = true; },
      detach: () => { attached = false; },
      sendCommand: async (method, parameters) => commands.push([method, parameters]),
    },
    executeJavaScript: async (script) => {
      scripts.push(script);
      if (script.includes("needsTrustedInput: true")) {
        return {ok: false, needsTrustedInput: true, error: "not sent", ...baseline};
      }
      if (script.includes("selectNodeContents")) return true;
      if (script.includes("submit_control_missing")) {
        return {ready: true, x: 320, y: 240, fingerprint: "BUTTON|composer-submit"};
      }
      if (script.includes("for (let tries = 0; tries < 80; tries += 1)")) {
        return {ok: true, needsTrustedInput: false, error: "", ...baseline};
      }
      answerChecks += 1;
      return {answer: "Native ChatGPT reply", changed: true, stopping: false, error: ""};
    },
  };
  const manager = new WebChatManager({
    electron: {}, owner: null,
    readSettings: () => ({}), writeSettings: () => {},
    shellPage: "file:///web-chat.html", shellPreload: "web-chat-shell-preload.js",
    answerPollMs: 2, answerDeadlineMs: 1000,
  });
  manager.connections.set("chatgpt-example", {
    id: "chatgpt-example", provider: "chatgpt", title: "ChatGPT",
    url: "https://chatgpt.com/c/example",
  });
  manager.viewFor = () => ({webContents: contents});
  manager.waitForLoad = async () => {};
  manager.attachFiles = async () => {};
  manager.rememberConnectionPage = () => false;
  manager.showCreatedConversationInOpenShells = () => {};

  const result = await manager.askNow("chatgpt-example", "Please answer natively", []);

  assert.equal(result.answer, "Native ChatGPT reply");
  assert.equal(attached, false);
  const inserted = commands.find(([method]) => method === "Input.insertText")?.[1]?.text;
  assert.match(inserted, /^\[NEXUS TRANSPORT TURN [0-9a-f-]{36}\]/);
  assert.ok(inserted.endsWith("Please answer natively"));
  assert.ok(scripts.some((script) => script.includes("setSelectionRange")));
  assert.deepEqual(commands.map(([method, parameters]) => [
    method, parameters.type || "", parameters.key || "",
  ]), [
    ["Input.insertText", "", ""],
    ["Input.dispatchMouseEvent", "mouseMoved", ""],
    ["Input.dispatchMouseEvent", "mousePressed", ""],
    ["Input.dispatchMouseEvent", "mouseReleased", ""],
  ]);
  assert.ok(answerChecks >= 3);
});

test("ChatGPT replaces a late-restored draft before any Send activation", async () => {
  const commands = [];
  let attached = false;
  let insertions = 0;
  let answerChecks = 0;
  const baseline = {
    beforeCount: 0, beforeLast: "", beforeUserCount: 0,
    beforeUserLast: "", beforeError: "", beforeStopping: false,
  };
  const contents = {
    isDestroyed: () => false, focus: () => {},
    debugger: {
      isAttached: () => attached, attach: () => { attached = true; },
      detach: () => { attached = false; },
      sendCommand: async (method, parameters) => {
        commands.push([method, parameters]);
        if (method === "Input.insertText") insertions += 1;
      },
    },
    executeJavaScript: async (script) => {
      if (script.includes("needsTrustedInput: true")) {
        return {ok: false, needsTrustedInput: true, error: "not sent", ...baseline};
      }
      if (script.includes("selectNodeContents")) return true;
      if (script.includes("submit_control_missing")) {
        return insertions === 1
          ? {ready: false, code: "composer_not_committed"}
          : {ready: true, x: 320, y: 240, fingerprint: "BUTTON|send-button"};
      }
      if (script.includes("for (let tries = 0; tries < 80; tries += 1)")) {
        return {ok: true, needsTrustedInput: false, error: "", ...baseline};
      }
      answerChecks += 1;
      return {answer: "HYDRATED_DRAFT_RECOVERED", changed: true, stopping: false, error: ""};
    },
  };
  const manager = new WebChatManager({
    electron: {}, owner: null,
    readSettings: () => ({}), writeSettings: () => {},
    shellPage: "file:///web-chat.html", shellPreload: "web-chat-shell-preload.js",
    answerPollMs: 1, answerDeadlineMs: 1000,
    submitReadyChecks: 2, submitAttempts: 2, submitPollMs: 1,
  });
  manager.connections.set("chatgpt-hydration", {
    id: "chatgpt-hydration", provider: "chatgpt", title: "ChatGPT",
    url: "https://chatgpt.com/",
  });
  manager.viewFor = () => ({webContents: contents});
  manager.waitForLoad = async () => {};
  manager.attachFiles = async () => {};
  manager.rememberConnectionPage = () => false;
  manager.showCreatedConversationInOpenShells = () => {};

  const result = await manager.askNow("chatgpt-hydration", "long marked Nexus prompt", []);

  assert.equal(result.answer, "HYDRATED_DRAFT_RECOVERED");
  assert.equal(insertions, 2);
  assert.equal(commands.filter(([method]) => method === "Input.dispatchMouseEvent").length, 3);
  assert.ok(answerChecks >= 3);
});
