"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {EventEmitter} = require("node:events");
const path = require("node:path");
const vm = require("node:vm");
const {
  PROVIDERS, WebChatManager, WebChatTurnError, allowedProviderUrl,
  specificConversationUrl, genericConversationUrl, providerReadinessScript,
  composerTextSelectionScript,
  automationScript, submissionBaselineScript, submitControlScript, submissionScript,
  answerScript, retryScript, stopScript,
  browserLikeUserAgent, authStorageAccessIsTrusted,
} = require("./web-chats");

test("a missing connection is a proven pre-submission failure", async () => {
  const manager = new WebChatManager({
    electron: {}, owner: null,
    readSettings: () => ({}), writeSettings: () => {},
    shellPage: "file:///web-chat.html", shellPreload: "web-chat-shell-preload.js",
  });
  await assert.rejects(
    () => manager.askNow("missing-connection", "test", []),
    (error) => error instanceof WebChatTurnError
      && error.deliveryState === "not_accepted"
      && error.failureCode === "connection_missing",
  );
});

test("provider pages and explicit sign-in hosts stay inside the remote browser boundary", () => {
  assert.equal(allowedProviderUrl(PROVIDERS.claude, "https://claude.ai/new"), true);
  assert.equal(allowedProviderUrl(PROVIDERS.claude, "https://accounts.google.com/o/oauth2/v2/auth", true), true);
  assert.equal(allowedProviderUrl(PROVIDERS.claude, "https://appleid.apple.com/auth/authorize", true), true);
  assert.equal(allowedProviderUrl(PROVIDERS.claude, "http://claude.ai/new"), false);
  assert.equal(allowedProviderUrl(PROVIDERS.claude, "https://claude.ai.attacker.example/"), false);
  const localFile = "file:" + "///" + ["C:", "Windows", "System32"].join("/");
  assert.equal(allowedProviderUrl(PROVIDERS.claude, localFile), false);
});

test("only provider-specific editable conversation URLs can own a Nexus chat", () => {
  const conversations = {
    chatgpt: "https://chatgpt.com/c/portable-chat",
    claude: "https://claude.ai/chat/portable-chat",
    gemini: "https://gemini.google.com/app/portable-chat",
    copilot: "https://copilot.microsoft.com/chats/portable-chat",
  };
  for (const [providerId, url] of Object.entries(conversations)) {
    assert.equal(specificConversationUrl(PROVIDERS[providerId], url), true, providerId);
    assert.equal(genericConversationUrl(
      PROVIDERS[providerId], PROVIDERS[providerId].newChat), true, providerId);
  }
  assert.equal(specificConversationUrl(
    PROVIDERS.chatgpt, "https://platform.openai.com/settings"), false);
  assert.equal(specificConversationUrl(
    PROVIDERS.claude, "https://claude.ai/settings/profile"), false);
  assert.equal(specificConversationUrl(
    PROVIDERS.gemini, "https://gemini.google.com/settings"), false);
  assert.equal(specificConversationUrl(
    PROVIDERS.copilot, "https://account.microsoft.com/"), false);
  assert.equal(allowedProviderUrl(
    PROVIDERS.gemini, "https://accounts.google.com/AccountChooser"), false);
  assert.equal(allowedProviderUrl(
    PROVIDERS.gemini, "https://accounts.google.com/AccountChooser", true), true);
  assert.equal(allowedProviderUrl(
    PROVIDERS.gemini, "https://www.google.com/search?q=nexus"), false);
});

test("restart drops legacy provider-page bindings that are not conversations", () => {
  const manager = new WebChatManager({
    electron: {}, owner: null,
    readSettings: () => ({webChats: [
      {id: "gemini-valid", provider: "gemini", title: "Valid",
        url: "https://gemini.google.com/app/portable-chat"},
      {id: "gemini-auth-page", provider: "gemini", title: "Wrong",
        url: "https://accounts.google.com/AccountChooser"},
      {id: "chatgpt-settings", provider: "chatgpt", title: "Wrong",
        url: "https://chatgpt.com/settings"},
    ]}),
    writeSettings: () => {},
    shellPage: "file:///web-chat.html", shellPreload: "web-chat-shell-preload.js",
  });

  assert.deepEqual(manager.list().map((one) => one.id), ["gemini-valid"]);
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

test("background web-chat mode is persisted and applied to external transports", async () => {
  let settings = {webChatBackgroundMode: true};
  const applied = [];
  let received = null;
  const manager = new WebChatManager({
    electron: {app: {getPath: () => "NexusData"}}, owner: null,
    readSettings: () => settings,
    writeSettings: (value) => { settings = value; },
    shellPage: "file:///web-chat.html", shellPreload: "web-chat-shell-preload.js",
    externalBrowserFactory: (options) => {
      received = options;
      return {setBackgroundMode: async (enabled) => applied.push(enabled)};
    },
  });
  manager.externalTransportFor("claude");

  assert.deepEqual(manager.preferences(), {backgroundMode: true});
  assert.equal(received.backgroundMode, true);

  assert.deepEqual(await manager.setBackgroundMode(false), {backgroundMode: false});
  assert.equal(settings.webChatBackgroundMode, false);
  assert.deepEqual(applied, [false]);
});

test("desktop settings write failures stay visible and background mode rolls back", async () => {
  const manager = new WebChatManager({
    electron: {app: {getPath: () => "NexusData"}}, owner: null,
    readSettings: () => ({webChatBackgroundMode: false}),
    writeSettings: () => { throw new Error("disk is read-only"); },
    shellPage: "file:///web-chat.html", shellPreload: "web-chat-shell-preload.js",
  });

  await assert.rejects(() => manager.setBackgroundMode(true), /disk is read-only/);
  assert.equal(manager.preferences().backgroundMode, false);
  assert.match(manager.preferences().persistence_error, /disk is read-only/);
});

test("a failed connection save is disclosed and a failed removal is rolled back", () => {
  const changed = [];
  let closed = 0;
  const manager = new WebChatManager({
    electron: {}, owner: {
      isDestroyed: () => false,
      webContents: {send: (...args) => changed.push(args)},
    },
    readSettings: () => ({}),
    writeSettings: () => { throw new Error("settings volume is full"); },
    shellPage: "file:///web-chat.html", shellPreload: "web-chat-shell-preload.js",
  });
  manager.connections.set("chatgpt-persist", {
    id: "chatgpt-persist", provider: "chatgpt", title: "Persist me",
    url: "https://chatgpt.com/c/persist", threads: {},
  });
  manager.views.set("chatgpt-persist", {webContents: {
    isDestroyed: () => false, close: () => { closed += 1; },
  }});

  assert.throws(() => manager.save(null, true), /settings volume is full/);
  assert.match(manager.list()[0].persistence_error, /settings volume is full/);
  assert.equal(changed.at(-1)[2], null);
  assert.throws(() => manager.remove("chatgpt-persist"), /settings volume is full/);
  assert.equal(manager.connections.has("chatgpt-persist"), true);
  assert.deepEqual(changed.at(-1)[1].map((one) => one.id), ["chatgpt-persist"]);
  assert.equal(changed.at(-1)[2], null);
  assert.equal(closed, 0);
  assert.equal(manager.views.has("chatgpt-persist"), true);
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
  assert.equal(state.markerSource, "user_selector");
});

test("Claude marked turns remain pairable when its user-message test id disappears", () => {
  const element = (text) => ({
    innerText: text, textContent: text, getClientRects: () => [1],
    getAttribute: () => "", contains: () => false,
  });
  const marker = "NEXUS TRANSPORT TURN claude-without-testid";
  const promptFragment = element(`[${marker}]`);
  const broadWrapper = element(`[${marker}]\nThe full board context`);
  const answer = element("CLAUDE_REPLY_AFTER_MARKER");
  const composer = element("");
  promptFragment.compareDocumentPosition = (node) => node === answer ? 4 : 2;
  broadWrapper.compareDocumentPosition = promptFragment.compareDocumentPosition;
  const began = {
    beforeCount: 0, beforeLast: "", beforeUserCount: 0, beforeUserLast: "",
    beforeError: "", submittedPrompt: `[${marker}]\nThe full board context`,
    submittedMarker: marker,
  };
  const context = {
    document: {querySelectorAll: (selector) => {
      if (selector === "[data-is-streaming] .standard-markdown") return [answer];
      if (selector === "body *") return [broadWrapper, promptFragment];
      if (PROVIDERS.claude.composer.includes(selector)) return [composer];
      return [];
    }},
    getComputedStyle: () => ({visibility: "visible"}),
  };

  const state = vm.runInNewContext(answerScript(PROVIDERS.claude, began), context);

  assert.equal(state.changed, true);
  assert.equal(state.answer, "CLAUDE_REPLY_AFTER_MARKER");
  assert.equal(state.markerSource, "broad_fallback");
});

test("Claude broad marker fallback cannot pair a stale following reply", () => {
  const element = (text) => ({
    innerText: text, textContent: text, getClientRects: () => [1],
    getAttribute: () => "", contains: () => false,
  });
  const marker = "NEXUS TRANSPORT TURN claude-stale-following-reply";
  const promptPreview = element(`[${marker}]\nDraft preview outside the editor`);
  const staleAnswer = element("CLAUDE_REPLY_FROM_BEFORE_SEND");
  const composer = element("");
  promptPreview.compareDocumentPosition = (node) => node === staleAnswer ? 4 : 2;
  const began = {
    beforeCount: 1, beforeLast: staleAnswer.innerText,
    beforeUserCount: 0, beforeUserLast: "", beforeError: "",
    submittedPrompt: `[${marker}]\nThe current request`, submittedMarker: marker,
  };
  const context = {
    document: {querySelectorAll: (selector) => {
      if (selector === "[data-is-streaming] .standard-markdown") return [staleAnswer];
      if (selector === "body *") return [promptPreview];
      if (PROVIDERS.claude.composer.includes(selector)) return [composer];
      return [];
    }},
    getComputedStyle: () => ({visibility: "visible"}),
  };

  const state = vm.runInNewContext(answerScript(PROVIDERS.claude, began), context);

  assert.equal(state.markerFound, true);
  assert.equal(state.markerSource, "broad_fallback");
  assert.equal(state.changed, false);
  assert.equal(state.answer, "");
});

test("a visible marker outside the composer stays uncertain without a paired reply", async () => {
  const visible = (text) => ({
    innerText: text, textContent: text, contains: () => false,
    getBoundingClientRect: () => ({width: 100, height: 30}),
  });
  const marker = "NEXUS TRANSPORT TURN accepted-without-testid";
  const prompt = `[${marker}]\nA long Nexus request`;
  const marked = visible(prompt);
  const composer = visible("");
  const began = {
    beforeCount: 0, beforeLast: "", beforeUserCount: 0, beforeUserLast: "",
    submittedMarker: marker, sendActivated: true,
  };
  const context = {
    document: {querySelectorAll: (selector) => {
      if (selector === "body *") return [marked];
      if (PROVIDERS.claude.composer.includes(selector)) return [composer];
      return [];
    }},
    getComputedStyle: () => ({visibility: "visible", display: "block"}),
    setTimeout: (resolve) => resolve(),
    HTMLTextAreaElement: class {}, HTMLInputElement: class {},
  };

  const state = await vm.runInNewContext(
    submissionScript(PROVIDERS.claude, prompt, began), context);

  assert.equal(state.ok, true);
  assert.equal(state.submissionState, "outcome_unknown");
});

test("an unsent Gemini draft cannot use its composer wrapper as a transport receipt", async () => {
  const marker = "NEXUS TRANSPORT TURN gemini-unsent-wrapper";
  const prompt = `[${marker}]\nCreate the requested files`;
  const visible = {
    getBoundingClientRect: () => ({width: 100, height: 30}),
    getClientRects: () => [1],
  };
  const composer = {
    ...visible, innerText: prompt, textContent: prompt,
    contains: (one) => one === composer,
  };
  const wrapper = {
    ...visible, innerText: prompt, textContent: prompt,
    contains: (one) => one === composer,
  };
  const send = {
    ...visible, disabled: false, hasAttribute: () => false,
    getAttribute: () => "false",
  };
  const began = {
    beforeCount: 0, beforeLast: "", beforeUserCount: 0, beforeUserLast: "",
    beforeError: "", submittedPrompt: prompt, submittedMarker: marker,
    sendActivated: true,
  };
  const context = {
    document: {
      visibilityState: "hidden",
      querySelectorAll: (selector) => {
        if (selector === "body *") return [wrapper, composer];
        if (PROVIDERS.gemini.composer.includes(selector)) return [composer];
        if (PROVIDERS.gemini.send.includes(selector)) return [send];
        return [];
      },
    },
    getComputedStyle: () => ({visibility: "visible", display: "block"}),
    setTimeout: (resolve) => resolve(),
    HTMLTextAreaElement: class {}, HTMLInputElement: class {},
  };

  const submission = await vm.runInNewContext(
    submissionScript(PROVIDERS.gemini, prompt, began), context);
  const answer = vm.runInNewContext(answerScript(PROVIDERS.gemini, began), context);

  assert.notEqual(submission.submissionState, "acknowledged");
  assert.equal(submission.submissionState, "not_accepted");
  assert.equal(submission.needsTrustedEnter, true);
  assert.equal(answer.markerFound, false);
  assert.equal(answer.changed, false);
  assert.equal(answer.userCount, 0);
  assert.equal(answer.replyCount, 0);
});

test("an unsent Claude draft cannot use its composer wrapper as a marker receipt", async () => {
  const marker = "NEXUS TRANSPORT TURN claude-unsent-wrapper";
  const prompt = `[${marker}]\nWait in the composer`;
  const visible = {
    getBoundingClientRect: () => ({width: 100, height: 30}),
    getClientRects: () => [1],
  };
  const composer = {
    ...visible, innerText: prompt, textContent: prompt,
    contains: (one) => one === composer,
  };
  const wrapper = {
    ...visible, innerText: prompt, textContent: prompt,
    contains: (one) => one === composer,
  };
  const began = {
    beforeCount: 0, beforeLast: "", beforeUserCount: 0, beforeUserLast: "",
    beforeError: "", submittedPrompt: prompt, submittedMarker: marker,
    sendActivated: true,
  };
  const context = {
    document: {querySelectorAll: (selector) => {
      if (selector === "body *") return [wrapper, composer];
      if (PROVIDERS.claude.composer.includes(selector)) return [composer];
      return [];
    }},
    getComputedStyle: () => ({visibility: "visible", display: "block"}),
    setTimeout: (resolve) => resolve(),
    HTMLTextAreaElement: class {}, HTMLInputElement: class {},
  };

  const submission = await vm.runInNewContext(
    submissionScript(PROVIDERS.claude, prompt, began), context);
  const answer = vm.runInNewContext(answerScript(PROVIDERS.claude, began), context);

  assert.notEqual(submission.submissionState, "acknowledged");
  assert.equal(answer.markerFound, false);
  assert.equal(answer.changed, false);
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

test("reconnecting a missing route preserves its exact ID and conversation key", async () => {
  let settings = {};
  const held = {
    providerId: "chatgpt",
    connectionId: "chatgpt-abcdef123456",
    conversationKey: "portable-board-chat-17",
    preferExisting: true,
    view: {webContents: {
      isLoading: () => false,
      executeJavaScript: async () => ({ready: true}),
      getURL: () => "https://chatgpt.com/c/reconnected-conversation",
      getTitle: async () => "Reconnected helper",
    }},
  };
  const manager = new WebChatManager({
    electron: {}, owner: null,
    readSettings: () => settings,
    writeSettings: (value) => { settings = value; },
    shellPage: "file:///web-chat.html",
    shellPreload: "web-chat-shell-preload.js",
  });
  manager.shellFor = () => held;

  const selected = await manager.useCurrent({});

  assert.equal(selected.id, "chatgpt-abcdef123456");
  assert.equal(held.connectionId, "chatgpt-abcdef123456");
  assert.equal(settings.webChats.length, 1);
  assert.equal(settings.webChats[0].id, "chatgpt-abcdef123456");
  assert.deepEqual(settings.webChats[0].threads["portable-board-chat-17"], {
    url: "https://chatgpt.com/c/reconnected-conversation",
    title: "Reconnected helper",
  });
});

test("an exact reconnect cannot reuse a connection owned by another provider", () => {
  const manager = new WebChatManager({
    electron: {}, owner: null,
    readSettings: () => ({webChats: [{
      id: "portable-route-17", provider: "gemini", title: "Gemini helper",
      url: "https://gemini.google.com/app/portable-conversation",
    }]}),
    writeSettings: () => {},
    shellPage: "file:///web-chat.html",
    shellPreload: "web-chat-shell-preload.js",
  });

  assert.throws(
    () => manager.openSetup("chatgpt", "portable-route-17", "portable-key", true),
    /belongs to a different provider/,
  );
});

test("a failed Use this chat save cannot leave a reachable ghost connection", async () => {
  const sent = [];
  const held = {
    providerId: "chatgpt", connectionId: "", view: {webContents: {
      isLoading: () => false,
      executeJavaScript: async () => ({ready: true}),
      getURL: () => "https://chatgpt.com/c/unsaved",
      getTitle: async () => "Unsaved helper",
    }},
  };
  const manager = new WebChatManager({
    electron: {},
    owner: {
      isDestroyed: () => false,
      webContents: {send: (...args) => sent.push(args)},
    },
    readSettings: () => ({}),
    writeSettings: () => { throw new Error("settings volume is full"); },
    shellPage: "file:///web-chat.html",
    shellPreload: "web-chat-shell-preload.js",
  });
  manager.shellFor = () => held;

  await assert.rejects(() => manager.useCurrent({}), /settings volume is full/);

  assert.equal(manager.connections.size, 0);
  assert.equal(held.connectionId, "");
  assert.deepEqual(sent.at(-1)[1], []);
  assert.equal(sent.at(-1)[2], null);
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

test("background navigation cannot silently rebind an owned provider conversation", () => {
  let settings = {webChats: [{
    id: "gemini-example", provider: "gemini", title: "Owned Gemini chat",
    url: "https://gemini.google.com/app/original-chat",
  }]};
  const manager = new WebChatManager({
    electron: {}, owner: null,
    readSettings: () => settings,
    writeSettings: (value) => { settings = value; },
    shellPage: "file:///web-chat.html", shellPreload: "web-chat-shell-preload.js",
  });
  const contents = Object.assign(new EventEmitter(), {
    url: "https://gemini.google.com/app/original-chat", title: "Original",
    getURL() { return this.url; }, getTitle() { return this.title; },
    isDestroyed: () => false,
  });
  manager.trackConnectionPage("gemini-example", contents);

  for (const drift of [
    "https://gemini.google.com/settings",
    "https://gemini.google.com/app/different-chat",
    "https://accounts.google.com/AccountChooser",
  ]) {
    contents.url = drift;
    contents.title = "Wrong page";
    contents.emit("did-navigate");
  }

  assert.equal(manager.connections.get("gemini-example").url,
    "https://gemini.google.com/app/original-chat");
  assert.equal(settings.webChats[0].url,
    "https://gemini.google.com/app/original-chat");
});

test("prefer-existing thread ownership rolls back when its durable save fails", () => {
  const changed = [];
  const manager = new WebChatManager({
    electron: {}, owner: {
      isDestroyed: () => false,
      webContents: {send: (...args) => changed.push(args)},
    },
    readSettings: () => ({}),
    writeSettings: () => { throw new Error("portable settings failure"); },
    shellPage: "file:///web-chat.html", shellPreload: "web-chat-shell-preload.js",
  });
  const connection = {
    id: "chatgpt-example", provider: "chatgpt", title: "Existing",
    url: "https://chatgpt.com/c/original-chat", threads: {},
  };
  manager.connections.set(connection.id, connection);

  assert.throws(
    () => manager.threadFor(connection, "pair-chat-portable", true),
    /portable settings failure/,
  );
  assert.equal(connection.threads["pair-chat-portable"], undefined);
  assert.deepEqual(changed.at(-1)[1].map((one) => one.id), [connection.id]);
  assert.equal(changed.at(-1)[2], null);
});

test("automatic conversation ownership rolls back when its durable save fails", () => {
  const changed = [];
  const manager = new WebChatManager({
    electron: {}, owner: {
      isDestroyed: () => false,
      webContents: {send: (...args) => changed.push(args)},
    },
    readSettings: () => ({}),
    writeSettings: () => { throw new Error("portable settings failure"); },
    shellPage: "file:///web-chat.html", shellPreload: "web-chat-shell-preload.js",
  });
  manager.connections.set("gemini-example", {
    id: "gemini-example", provider: "gemini", title: "Gemini",
    url: PROVIDERS.gemini.newChat, threads: {},
  });
  const contents = {
    isDestroyed: () => false,
    getURL: () => "https://gemini.google.com/app/new-owned-chat",
    getTitle: () => "New owned chat",
  };

  assert.throws(
    () => manager.rememberConnectionPage("gemini-example", contents),
    /portable settings failure/,
  );
  assert.equal(manager.connections.get("gemini-example").url, PROVIDERS.gemini.newChat);
  assert.equal(changed.at(-1)[1][0].url, PROVIDERS.gemini.newChat);
  assert.equal(changed.at(-1)[2], null);
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

test("Start new keeps the durable binding when provider navigation fails", async () => {
  let settings = {webChats: [{
    id: "gemini-example", provider: "gemini", title: "Gemini",
    url: "https://gemini.google.com/app/base", threads: {
      "pair-chat": {url: "https://gemini.google.com/app/owned", title: "Owned"},
    },
  }]};
  const manager = new WebChatManager({
    electron: {}, owner: null,
    readSettings: () => settings,
    writeSettings: (value) => { settings = value; },
    shellPage: "file:///web-chat.html", shellPreload: "web-chat-shell-preload.js",
  });
  const remote = {
    isLoading: () => false,
    loadURL: async () => { throw new Error("provider navigation failed"); },
  };
  manager.shellFor = () => ({
    providerId: "gemini", connectionId: "gemini-example",
    conversationKey: "pair-chat", view: {webContents: remote},
  });

  await assert.rejects(() => manager.startNew({}), /provider navigation failed/);

  assert.equal(manager.connections.get("gemini-example").threads["pair-chat"].url,
    "https://gemini.google.com/app/owned");
  assert.equal(settings.webChats[0].threads["pair-chat"].url,
    "https://gemini.google.com/app/owned");
});

test("Start new restores the binding and visible route when its save fails", async () => {
  const loads = [];
  let url = "https://gemini.google.com/app/owned";
  const manager = new WebChatManager({
    electron: {}, owner: null,
    readSettings: () => ({}),
    writeSettings: () => { throw new Error("settings are unwritable"); },
    shellPage: "file:///web-chat.html", shellPreload: "web-chat-shell-preload.js",
  });
  manager.connections.set("gemini-example", {
    id: "gemini-example", provider: "gemini", title: "Gemini",
    url: "https://gemini.google.com/app/base", threads: {
      "pair-chat": {url, title: "Owned"},
    },
  });
  const remote = {
    isLoading: () => false, getURL: () => url,
    loadURL: async (next) => { loads.push(next); url = next; },
  };
  manager.shellFor = () => ({
    providerId: "gemini", connectionId: "gemini-example",
    conversationKey: "pair-chat", view: {webContents: remote},
  });

  await assert.rejects(() => manager.startNew({}), /settings are unwritable/);

  assert.equal(manager.connections.get("gemini-example").threads["pair-chat"].url,
    "https://gemini.google.com/app/owned");
  assert.deepEqual(loads, [
    PROVIDERS.gemini.newChat, "https://gemini.google.com/app/owned",
  ]);
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

test("hiding an embedded provider always returns native keyboard focus to the board", () => {
  const focusOrder = [];
  const moved = [];
  const view = {webContents: {isDestroyed: () => false}};
  const manager = new WebChatManager({
    electron: {},
    owner: {
      isDestroyed: () => false,
      focus: () => focusOrder.push("window"),
      webContents: {focus: () => focusOrder.push("board")},
    },
    readSettings: () => ({}), writeSettings: () => {},
    shellPage: "file:///web-chat.html", shellPreload: "web-chat-shell-preload.js",
  });
  manager.views.set("gemini-example", view);
  manager.activeEmbedded = "gemini-example";
  manager.parkBackgroundView = (one) => moved.push(one);

  assert.equal(manager.hideEmbedded(), true);
  assert.deepEqual(moved, [view]);
  assert.deepEqual(focusOrder, ["window", "board"]);
  assert.equal(manager.activeEmbedded, "");

  assert.equal(manager.hideEmbedded(), false);
  assert.deepEqual(focusOrder, ["window", "board", "window", "board"]);
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

test("preflight refuses a drifted conversation before any provider submission", async () => {
  let scripts = 0;
  let closed = 0;
  const contents = {
    isLoading: () => false, isDestroyed: () => false,
    getURL: () => "https://gemini.google.com/app/different-chat",
    getTitle: () => "Different",
    executeJavaScript: async () => { scripts += 1; return {}; },
    close: () => { closed += 1; },
  };
  const view = {webContents: contents};
  const manager = new WebChatManager({
    electron: {}, owner: null,
    readSettings: () => ({}), writeSettings: () => {},
    shellPage: "file:///web-chat.html", shellPreload: "web-chat-shell-preload.js",
  });
  manager.connections.set("gemini-example", {
    id: "gemini-example", provider: "gemini", title: "Gemini",
    url: "https://gemini.google.com/app/owned-chat", threads: {},
  });
  manager.views.set("gemini-example", view);

  await assert.rejects(
    () => manager.askNow("gemini-example", "Do not misdeliver", []),
    (error) => error instanceof WebChatTurnError
      && error.deliveryState === "not_accepted"
      && error.failureCode === "conversation_binding_drift",
  );
  assert.equal(scripts, 0);
  assert.equal(closed, 1);
  assert.equal(manager.views.has("gemini-example"), false);
});

test("preflight refuses a newly discovered chat when its binding cannot be saved", async () => {
  let submissions = 0;
  const contents = {
    isLoading: () => false, isDestroyed: () => false,
    getURL: () => "https://gemini.google.com/app/discovered-chat",
    getTitle: () => "Discovered",
    executeJavaScript: async () => { submissions += 1; return {}; },
    close: () => {},
  };
  const manager = new WebChatManager({
    electron: {}, owner: null,
    readSettings: () => ({}),
    writeSettings: () => { throw new Error("settings are unwritable"); },
    shellPage: "file:///web-chat.html", shellPreload: "web-chat-shell-preload.js",
  });
  manager.connections.set("gemini-example", {
    id: "gemini-example", provider: "gemini", title: "Gemini",
    url: PROVIDERS.gemini.newChat, threads: {},
  });
  const view = {webContents: contents};
  manager.views.set("gemini-example", view);

  await assert.rejects(
    () => manager.askNow("gemini-example", "Do not send without a route", []),
    (error) => error instanceof WebChatTurnError
      && error.deliveryState === "not_accepted"
      && error.failureCode === "conversation_binding_not_saved",
  );
  assert.equal(submissions, 0);
  assert.equal(manager.connections.get("gemini-example").url, PROVIDERS.gemini.newChat);
});

test("a never-loading page times out before submission and the next queued turn recovers", async () => {
  let created = 0;
  let firstClosed = 0;
  const stuck = Object.assign(new EventEmitter(), {
    isLoading: () => true, isDestroyed: () => false,
    getURL: () => "https://gemini.google.com/app/owned-chat",
    getTitle: () => "Owned",
    loadURL: async () => {}, close: () => { firstClosed += 1; },
  });
  const recovered = Object.assign(new EventEmitter(), {
    isLoading: () => false, isDestroyed: () => false,
    getURL: () => "https://gemini.google.com/app/owned-chat",
    getTitle: () => "Gemini",
    loadURL: async () => {}, close: () => {},
    executeJavaScript: async (script) => script.includes("const prompt =")
      ? {ok: true, submissionState: "acknowledged", beforeCount: 0, beforeLast: ""}
      : {answer: "Recovered answer", changed: true, stopping: false, error: ""},
  });
  const manager = new WebChatManager({
    electron: {}, owner: null,
    readSettings: () => ({}), writeSettings: () => {},
    shellPage: "file:///web-chat.html", shellPreload: "web-chat-shell-preload.js",
    preSubmitDeadlineMs: 15, answerPollMs: 1, answerDeadlineMs: 1000,
  });
  manager.connections.set("gemini-example", {
    id: "gemini-example", provider: "gemini", title: "Gemini",
    url: "https://gemini.google.com/app/owned-chat", threads: {},
  });
  manager.makeRemoteView = () => ({webContents: created++ === 0 ? stuck : recovered});
  manager.parkBackgroundView = () => {};

  await assert.rejects(
    () => manager.ask("gemini-example", "First turn", []),
    (error) => error instanceof WebChatTurnError
      && error.deliveryState === "not_accepted"
      && error.failureCode === "pre_submission_timeout",
  );
  const result = await manager.ask("gemini-example", "Second turn", []);

  assert.equal(firstClosed, 1);
  assert.equal(created, 2);
  assert.equal(result.answer, "Recovered answer");
});

test("Stop wakes a pre-submit load wait without waiting for its deadline", async () => {
  let closed = 0;
  const stuck = Object.assign(new EventEmitter(), {
    isLoading: () => true, isDestroyed: () => false,
    getURL: () => "https://gemini.google.com/app/owned-chat",
    loadURL: async () => {}, close: () => { closed += 1; },
  });
  const manager = new WebChatManager({
    electron: {}, owner: null,
    readSettings: () => ({}), writeSettings: () => {},
    shellPage: "file:///web-chat.html", shellPreload: "web-chat-shell-preload.js",
    preSubmitDeadlineMs: 5000,
  });
  manager.connections.set("gemini-example", {
    id: "gemini-example", provider: "gemini", title: "Gemini",
    url: "https://gemini.google.com/app/owned-chat", threads: {},
  });
  manager.makeRemoteView = () => ({webContents: stuck});
  manager.parkBackgroundView = () => {};

  const turn = manager.ask("gemini-example", "Cancel before send", []);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(await manager.stop("gemini-example"), true);
  await assert.rejects(
    () => turn,
    (error) => error instanceof WebChatTurnError
      && error.deliveryState === "not_accepted"
      && error.failureCode === "pre_submission_cancelled",
  );
  assert.equal(closed, 1);
  assert.equal(manager.activeAsks.has("gemini-example"), false);
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
    /did not observe a finished visible reply/,
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

async function uncertainChatGptAnswerTimeout(markerFound, answer = "Incomplete answer") {
  let stops = 0;
  const contents = {
    isDestroyed: () => false,
    getURL: () => PROVIDERS.chatgpt.home,
    executeJavaScript: async (script) => {
      if (script.includes("const prompt =")) {
        return {
          ok: true, submissionState: "outcome_unknown",
          beforeCount: 0, beforeLast: "", beforeUserCount: 0,
          beforeUserLast: "", beforeError: "", beforeStopping: false,
        };
      }
      if (script.includes("const began =")) {
        return {
          answer, changed: Boolean(answer), stopping: true, error: "",
          markerFound, replyCount: 1, userCount: markerFound ? 1 : 0,
          visibility: "visible",
        };
      }
      stops += 1;
      return true;
    },
  };
  const manager = new WebChatManager({
    electron: {}, owner: null,
    readSettings: () => ({}), writeSettings: () => {},
    shellPage: "file:///web-chat.html", shellPreload: "web-chat-shell-preload.js",
    answerPollMs: 1, answerDeadlineMs: 1000,
  });
  // Keep the regression fast without changing the production lower bound.
  manager.answerDeadlineMs = 30;
  manager.connections.set("chatgpt-example", {
    id: "chatgpt-example", provider: "chatgpt", title: "ChatGPT", url: PROVIDERS.chatgpt.home,
  });
  manager.viewFor = () => ({webContents: contents});
  manager.waitForLoad = async () => {};
  manager.attachFiles = async () => {};
  manager.rememberConnectionPage = () => false;
  manager.showCreatedConversationInOpenShells = () => {};

  let failure;
  await assert.rejects(
    () => manager.askNow("chatgpt-example", "Please answer", []),
    (error) => {
      failure = error;
      return error instanceof WebChatTurnError;
    },
  );
  return {failure, stops};
}

test("a late ChatGPT transport marker promotes an uncertain send before answer timeout", async () => {
  const {failure, stops} = await uncertainChatGptAnswerTimeout(true);

  assert.equal(failure.deliveryState, "accepted");
  assert.equal(failure.failureCode, "reply_completion_timeout");
  assert.equal(failure.diagnostics.submission_state, "acknowledged");
  assert.equal(failure.diagnostics.marker_found, true);
  assert.equal(failure.diagnostics.marked_reply_found, true);
  assert.equal(stops, 1);
});

test("an uncertain ChatGPT answer timeout stays conservative without its transport marker", async () => {
  const {failure, stops} = await uncertainChatGptAnswerTimeout(false);

  assert.equal(failure.deliveryState, "unknown");
  assert.equal(failure.failureCode, "turn_match_unknown");
  assert.equal(failure.diagnostics.submission_state, "outcome_unknown");
  assert.equal(failure.diagnostics.marker_found, false);
  assert.equal(failure.diagnostics.marked_reply_found, false);
  assert.equal(stops, 1);
});

test("a ChatGPT marker without a causally paired reply does not clear uncertainty", async () => {
  const {failure, stops} = await uncertainChatGptAnswerTimeout(true, "");

  assert.equal(failure.deliveryState, "unknown");
  assert.equal(failure.failureCode, "turn_match_unknown");
  assert.equal(failure.diagnostics.submission_state, "outcome_unknown");
  assert.equal(failure.diagnostics.marker_found, true);
  assert.equal(failure.diagnostics.marked_reply_found, false);
  assert.equal(stops, 1);
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
    /did not observe a finished visible reply/,
  );
  assert.equal(stops, 1);
});

test("a Stop control already visible before submission still prevents a partial reply commit", async () => {
  let answerChecks = 0;
  let stops = 0;
  const contents = {
    isDestroyed: () => false,
    executeJavaScript: async (script) => {
      if (script.includes("const prompt =")) {
        return {
          ok: true, submissionState: "acknowledged",
          beforeCount: 0, beforeLast: "", beforeError: "", beforeStopping: true,
        };
      }
      if (script.includes("const began =")) {
        answerChecks += 1;
        return {answer: "Finished answer", changed: true, stopping: true, error: ""};
      }
      stops += 1;
      return true;
    },
  };
  const manager = new WebChatManager({
    electron: {}, owner: null,
    readSettings: () => ({}), writeSettings: () => {},
    shellPage: "file:///web-chat.html", shellPreload: "web-chat-shell-preload.js",
    answerPollMs: 2, answerDeadlineMs: 1000, staleStopGraceMs: 6,
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
    /did not observe a finished visible reply/,
  );
  assert.ok(answerChecks >= 4);
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
