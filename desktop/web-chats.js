"use strict";

const crypto = require("node:crypto");
const path = require("node:path");
const {ExternalBrowserTransport} = require("./external-browser");

const PROVIDERS = Object.freeze({
  chatgpt: {
    id: "chatgpt", label: "ChatGPT", home: "https://chatgpt.com/", newChat: "https://chatgpt.com/",
    hosts: ["chatgpt.com", "openai.com"],
    authHosts: ["auth0.openai.com", "accounts.google.com", "login.microsoftonline.com", "appleid.apple.com"],
    readyComposer: ["#prompt-textarea", "#mobile-composer-prompt", "textarea[data-mobile-composer-prompt]"],
    composer: ["#prompt-textarea", "#mobile-composer-prompt", "textarea[data-mobile-composer-prompt]", "[contenteditable='true'][data-placeholder]", "textarea"],
    send: ["button[data-testid='send-button']", "button[data-composer-submit]", "button[aria-label='Send message']", "button[aria-label*='Send']"],
    users: ["[data-message-author-role='user']", "[data-user-message-bubble]"],
    replies: ["[data-message-author-role='assistant']"],
    stop: ["button[data-testid='stop-button']", "button[data-composer-submit][aria-label*='Stop']", "button[aria-label*='Stop']"],
    errors: ["button[data-conversation-recovery-retry][aria-description]", "[role='alert']"],
    retry: ["button[data-conversation-recovery-retry]"], retryVisibleError: true,
    signedOut: [".wm-app-loginButton", ".wm-sidebar-loginButton", "[data-mobile-auth-entry-action='login_or_signup']"],
    signedOutPaths: ["/auth/", "/login", "/signup"],
    attach: ["button[aria-label*='Attach']", "button[data-testid*='attach']"],
    pairRepliesToUsers: true, acceptComposerClear: true,
  },
  claude: {
    id: "claude", label: "Claude", home: "https://claude.ai/", newChat: "https://claude.ai/new",
    hosts: ["claude.ai", "anthropic.com"],
    authHosts: ["accounts.google.com", "login.microsoftonline.com", "appleid.apple.com"],
    browserLikeUserAgent: true,
    // Claude's current Cloudflare policy repeatedly challenges embedded or
    // automation-launched Chromium. Use an ordinary installed browser in a
    // Nexus-owned profile, then attach over a loopback-only control channel.
    externalBrowser: {preferred: ["chrome", "edge"]},
    authStorageHosts: ["hcaptcha.com", "challenges.cloudflare.com", "cloudflare.com"],
    readyComposer: ["div.ProseMirror[contenteditable='true']"],
    composer: ["div.ProseMirror[contenteditable='true']", "[contenteditable='true']", "textarea"],
    send: ["button[aria-label='Send message']", "button[aria-label*='Send']", "button[data-testid*='send']"],
    users: ["[data-testid='user-message']", "[data-testid*='user-message']"],
    replies: ["[data-testid='assistant-message']", "[data-testid*='assistant']", ".font-claude-message", ".prose"],
    stop: ["button[aria-label*='Stop']", "button[data-testid*='stop']"],
    errors: ["[role='alert']", "[data-testid*='error']"],
    signedOut: ["form[action*='login']", "a[href*='/login']"],
    signedOutPaths: ["/login", "/oauth", "/onboarding"],
    attach: ["button[aria-label*='Add content']", "button[aria-label*='Attach']"],
    pairRepliesToUsers: true,
  },
  gemini: {
    id: "gemini", label: "Gemini", home: "https://gemini.google.com/app", newChat: "https://gemini.google.com/app",
    hosts: ["gemini.google.com", "google.com"], authHosts: ["accounts.google.com"],
    readyComposer: ["rich-textarea [contenteditable='true']", ".ql-editor[contenteditable='true']"],
    composer: ["rich-textarea [contenteditable='true']", ".ql-editor[contenteditable='true']", "textarea"],
    send: ["button[aria-label*='Send']", ".send-button"],
    users: ["user-query", ".user-query", "[data-message-author-role='user']"],
    replies: ["message-content", ".model-response-text", ".response-content"],
    stop: ["button[aria-label*='Stop']", ".stop-button"],
    errors: ["[role='alert']", ".error-message"],
    attach: ["button[aria-label*='Upload']", "button[aria-label*='Add file']"],
    retryStalledReply: true, maxStallRetries: 3, settleWhileStoppingMs: 8000,
    pairRepliesToUsers: true, acceptComposerClear: true,
  },
  copilot: {
    id: "copilot", label: "Microsoft Copilot", home: "https://copilot.microsoft.com/", newChat: "https://copilot.microsoft.com/",
    hosts: ["copilot.microsoft.com", "microsoft.com"],
    authHosts: ["login.live.com", "login.microsoftonline.com", "account.microsoft.com"],
    readyComposer: ["textarea[data-testid*='composer']", "textarea", "[contenteditable='true']"],
    composer: ["textarea[data-testid*='composer']", "textarea", "[contenteditable='true']"],
    send: ["button[aria-label*='Submit']", "button[aria-label*='Send']", "button[data-testid*='send']"],
    users: ["[data-content='user-message']", "[data-testid*='user-message']"],
    replies: ["[data-content='ai-message']", "[data-testid*='assistant']", ".ac-container"],
    stop: ["button[aria-label*='Stop']", "button[data-testid*='stop']"],
    errors: ["[role='alert']", "[data-testid*='error']"],
    signedOut: ["[data-testid='anonymous-block-page-title']", "a[href*='login.live.com']"],
    attach: ["button[aria-label*='Attach']", "button[aria-label*='Add']"],
    pairRepliesToUsers: true,
  },
});

function hostMatches(host, allowed) {
  return host === allowed || host.endsWith(`.${allowed}`);
}

function allowedProviderUrl(provider, candidate, includeAuth = false) {
  try {
    const parsed = new URL(candidate);
    if (parsed.protocol !== "https:") return false;
    const hosts = [...provider.hosts, ...(includeAuth ? provider.authHosts : [])];
    return hosts.some((one) => hostMatches(parsed.hostname.toLowerCase(), one));
  } catch (_error) {
    return false;
  }
}

function browserLikeUserAgent(value) {
  // Claude's invisible hCaptcha classifies Electron's product tokens as an
  // automated embedded client and can leave Continue spinning forever. The
  // provider still sees the real Chromium version and platform; only the app
  // shell/product tokens are removed, as ordinary Chromium wrappers do.
  return String(value || "")
    .replace(/\s+(?:Electron|our-harness-desktop)\/[^\s]+/gi, "")
    .replace(/\s{2,}/g, " ").trim();
}

function authStorageAccessIsTrusted(provider, permission, origins = []) {
  if (!new Set(["storage-access", "top-level-storage-access"]).has(permission)) return false;
  // Auth challenge implementations change independently of Nexus. Providers
  // declare the narrowly trusted storage origins instead of relying on one
  // global CAPTCHA vendor exception.
  const trusted = provider?.authStorageHosts || [];
  return origins.some((candidate) => {
    try {
      const host = new URL(String(candidate || "")).hostname.toLowerCase();
      return trusted.some((one) => hostMatches(host, one));
    } catch (_error) {
      return false;
    }
  });
}

function publicConnection(one) {
  return {
    id: one.id, provider: one.provider, title: one.title, url: one.url,
    external: Boolean(PROVIDERS[one.provider]?.externalBrowser),
  };
}

function cleanConversationKey(value) {
  const key = String(value || "");
  return /^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$/.test(key) ? key : "";
}

function channelKey(id, conversationKey = "") {
  const key = cleanConversationKey(conversationKey);
  return key ? `${String(id)}\n${key}` : String(id);
}

function specificConversationUrl(provider, candidate) {
  if (!allowedProviderUrl(provider, candidate)) return false;
  try {
    const actual = new URL(candidate);
    const generic = [provider.home, provider.newChat].map((one) => new URL(one));
    return !generic.some((one) => (
      actual.origin === one.origin
      && actual.pathname.replace(/\/+$/, "") === one.pathname.replace(/\/+$/, "")
    ));
  } catch (_error) {
    return false;
  }
}

function savedConnection(one) {
  const value = publicConnection(one);
  const threads = Object.fromEntries(Object.entries(one.threads || {}).map(([key, thread]) => [
    key, {url: thread.url, title: thread.title || ""},
  ]));
  if (Object.keys(threads).length) value.threads = threads;
  return value;
}

function providerReadinessScript(provider) {
  return `(() => {
    const provider = ${JSON.stringify({
      label: provider.label, readyComposer: provider.readyComposer || provider.composer,
      signedOut: provider.signedOut || [], signedOutPaths: provider.signedOutPaths || [],
      external: Boolean(provider.externalBrowser),
    })};
    const reasons = ${JSON.stringify({
      challenge: `${provider.label}'s browser check is still running. Wait for it to finish, then try again.`,
      signedOut: provider.externalBrowser
        ? `Sign in to ${provider.label} in the secure Nexus browser window first.`
        : `Sign in to ${provider.label} inside this Nexus window first. A login in your normal browser is separate.`,
      notReady: `${provider.label} is not showing a usable chat yet. Finish signing in, then start or open a chat.`,
    })};
    const visible = (one) => {
      if (!one || getComputedStyle(one).visibility === "hidden" || getComputedStyle(one).display === "none") return false;
      const rect = one.getBoundingClientRect();
      return rect.width > 1 && rect.height > 1;
    };
    const path = location.pathname.toLowerCase();
    const signedOut = provider.signedOutPaths.some((one) => path.startsWith(one))
      || provider.signedOut.some((selector) => [...document.querySelectorAll(selector)].some(visible));
    const challenged = /just a moment|verify you are human|checking your browser/i.test(
      (document.title || "") + " " + (document.body?.innerText || ""));
    const composer = provider.readyComposer
      .flatMap((selector) => [...document.querySelectorAll(selector)]).find(visible);
    if (challenged) return {ready: false, reason: reasons.challenge};
    if (signedOut) return {ready: false, reason: reasons.signedOut};
    if (!composer) return {ready: false, reason: reasons.notReady};
    return {ready: true};
  })()`;
}

function automationScript(provider, prompt, submittedMarker = "") {
  // Only fixed selectors and JSON-encoded text enter the remote page. The
  // renderer cannot send JavaScript across IPC.
  const notSent = `Nexus filled ${provider.label}'s message box, but the provider did not accept Send. Open the provider chat and try again.`;
  return `(async () => {
    const selectors = ${JSON.stringify({
      composer: provider.composer, send: provider.send,
      users: provider.users || [], replies: provider.replies, stop: provider.stop,
      errors: provider.errors || [], signedOut: provider.signedOut || [],
      signedOutPaths: provider.signedOutPaths || [],
      pairRepliesToUsers: Boolean(provider.pairRepliesToUsers),
      acceptComposerClear: Boolean(provider.acceptComposerClear),
    })};
    const prompt = ${JSON.stringify(String(prompt || ""))};
    const submittedMarker = ${JSON.stringify(String(submittedMarker || ""))};
    const visible = (one) => {
      if (!one || getComputedStyle(one).visibility === "hidden" || getComputedStyle(one).display === "none") return false;
      const rect = one.getBoundingClientRect();
      return rect.width > 1 && rect.height > 1;
    };
    const first = (list) => list.flatMap((selector) => [...document.querySelectorAll(selector)]).find(visible);
    const values = (list, accessible = false) => {
      for (const selector of list) {
        const values = [...document.querySelectorAll(selector)].filter(visible)
          .map((one) => ((accessible && one.getAttribute("aria-description"))
            || one.innerText || one.textContent || "").trim()).filter(Boolean);
        if (values.length) return values;
      }
      return [];
    };
    const normal = (value) => String(value || "").replace(/\\s+/g, " ").trim();
    const promptMatches = (value) => {
      const rendered = normal(value);
      const submitted = normal(prompt);
      if (!rendered || !submitted) return false;
      if (rendered.includes(submitted)) return true;
      // Provider bubbles may reflow or abbreviate a very long Nexus context.
      // Requiring both ends still identifies this exact turn without accepting
      // an unrelated old bubble that merely appeared after virtualisation.
      const head = submitted.slice(0, Math.min(180, submitted.length));
      const tail = submitted.slice(Math.max(0, submitted.length - 180));
      return head.length >= 40 && tail.length >= 40
        && rendered.includes(head) && rendered.includes(tail);
    };
    const path = location.pathname.toLowerCase();
    const signedOut = selectors.signedOutPaths.some((one) => path.startsWith(one))
      || selectors.signedOut.some((selector) => [...document.querySelectorAll(selector)].some(visible));
    if (signedOut) return {ok: false, error: ${JSON.stringify(
      provider.externalBrowser
        ? `Sign in to ${provider.label} in the secure Nexus browser window first.`
        : `Sign in to ${provider.label} inside this Nexus window first. A login in your normal browser is separate.`)}};
    const composer = first(selectors.composer);
    if (!composer) return {ok: false, error: "Nexus could not find this provider's message box. Open a chat, then try again."};
    const before = values(selectors.replies);
    const beforeUsers = values(selectors.users);
    const beforeErrors = values(selectors.errors, true);
    const beforeStopping = selectors.stop.some(
      (selector) => [...document.querySelectorAll(selector)].some(visible));
    composer.focus();
    if (composer instanceof HTMLTextAreaElement || composer instanceof HTMLInputElement) {
      const descriptor = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(composer), "value");
      if (descriptor && descriptor.set) descriptor.set.call(composer, prompt); else composer.value = prompt;
    } else {
      // ProseMirror and Quill update their framework state through the browser
      // editing pipeline. Directly replacing children can look correct for a
      // short prompt yet be reverted before a long Nexus prompt enables Send.
      const inserted = typeof document.execCommand === "function"
        && document.execCommand("insertText", false, prompt);
      if (!inserted) composer.replaceChildren(document.createTextNode(prompt));
    }
    composer.dispatchEvent(new InputEvent("input", {bubbles: true, inputType: "insertText", data: prompt}));
    composer.dispatchEvent(new Event("change", {bubbles: true}));
    let send = null;
    // Provider editors commit long text asynchronously. Wait until the live
    // editor still contains this exact turn and its send control is mounted,
    // instead of clicking at a fixed time while framework state is stale.
    for (let tries = 0; tries < 50; tries += 1) {
      await new Promise((resolve) => setTimeout(resolve, 100));
      const liveComposer = first(selectors.composer);
      const liveText = normal(liveComposer instanceof HTMLTextAreaElement
        || liveComposer instanceof HTMLInputElement
        ? liveComposer.value : (liveComposer?.innerText || liveComposer?.textContent || ""));
      send = first(selectors.send);
      if (send && promptMatches(liveText)) break;
    }
    // Gemini's current send control is a <gem-icon-button> custom element.
    // Clicking its inner Material button changes no Angular state; clicking
    // the host dispatches the component's real submit action. Ordinary wrapper
    // elements still use their concrete button descendant.
    const customElement = Boolean(send?.tagName?.includes("-"));
    const clickTarget = customElement || send?.matches("button, [role='button']")
      ? send : send?.querySelector("button, [role='button']");
    const disabled = clickTarget && (clickTarget.disabled
      || clickTarget.getAttribute("aria-disabled") === "true"
      || clickTarget.hasAttribute("data-visually-disabled"));
    let sendActivated = false;
    if (clickTarget && !disabled) {
      clickTarget.click();
      sendActivated = true;
    } else {
      composer.dispatchEvent(new KeyboardEvent("keydown", {key: "Enter", code: "Enter", bubbles: true, cancelable: true}));
    }
    let submitted = false;
    for (let tries = 0; tries < 80 && !submitted; tries += 1) {
      await new Promise((resolve) => setTimeout(resolve, 100));
      const after = values(selectors.replies);
      const afterUsers = values(selectors.users);
      const newlyRenderedUsers = afterUsers.slice(beforeUsers.length);
      const userAdvanced = newlyRenderedUsers.some(promptMatches)
        || Boolean(afterUsers.at(-1) && afterUsers.at(-1) !== beforeUsers.at(-1)
          && promptMatches(afterUsers.at(-1)));
      const replyAdvanced = after.length > before.length
        || (after.at(-1) && after.at(-1) !== before.at(-1));
      const stoppingNow = selectors.stop.some(
        (selector) => [...document.querySelectorAll(selector)].some(visible));
      const composerText = normal(
        composer instanceof HTMLTextAreaElement || composer instanceof HTMLInputElement
          ? composer.value : (composer.innerText || composer.textContent || ""));
      // Gemini's custom send component clears its Quill editor as soon as the
      // turn is accepted, while the virtualised user-query node can mount more
      // than eight seconds later. This signal is scoped to providers that
      // explicitly guarantee clear-on-submit; answerScript still pairs the
      // eventual reply to the exact user bubble before returning any text.
      const composerAccepted = selectors.acceptComposerClear && sendActivated && !composerText;
      // Gemini exposes a Stop control for a previous turn while its UI is
      // settling. That old control is not acknowledgement of this prompt.
      // Pair-aware providers must render a new user turn; other providers may
      // also acknowledge through a new reply or a newly appearing Stop.
      submitted = userAdvanced || composerAccepted || (!selectors.pairRepliesToUsers
        && (replyAdvanced || (!beforeStopping && stoppingNow)));
    }
    const baseline = {
      beforeCount: before.length, beforeLast: before.at(-1) || "",
      beforeUserCount: beforeUsers.length, beforeUserLast: beforeUsers.at(-1) || "",
      beforeError: beforeErrors.at(-1) || "", beforeStopping, sendActivated,
      submittedPrompt: prompt, submittedMarker,
    };
    if (!submitted) return {ok: false, needsTrustedEnter: true, error: ${JSON.stringify(notSent)}, ...baseline};
    return {ok: true, ...baseline};
  })()`;
}

function submissionScript(provider, prompt, began) {
  return `(async () => {
    const selectors = ${JSON.stringify({
      users: provider.users || [], replies: provider.replies, stop: provider.stop,
      pairRepliesToUsers: Boolean(provider.pairRepliesToUsers),
    })};
    const prompt = ${JSON.stringify(String(prompt || ""))};
    const began = ${JSON.stringify(began)};
    const visible = (one) => {
      if (!one || getComputedStyle(one).visibility === "hidden" || getComputedStyle(one).display === "none") return false;
      const rect = one.getBoundingClientRect();
      return rect.width > 1 && rect.height > 1;
    };
    const values = (list) => {
      for (const selector of list) {
        const found = [...document.querySelectorAll(selector)].filter(visible)
          .map((one) => (one.innerText || one.textContent || "").trim()).filter(Boolean);
        if (found.length) return found;
      }
      return [];
    };
    const normal = (value) => String(value || "").replace(/\\s+/g, " ").trim();
    const promptMatches = (value) => {
      const rendered = normal(value);
      const submitted = normal(prompt);
      if (!rendered || !submitted) return false;
      if (rendered.includes(submitted)) return true;
      const head = submitted.slice(0, Math.min(180, submitted.length));
      const tail = submitted.slice(Math.max(0, submitted.length - 180));
      return head.length >= 40 && tail.length >= 40
        && rendered.includes(head) && rendered.includes(tail);
    };
    for (let tries = 0; tries < 80; tries += 1) {
      await new Promise((resolve) => setTimeout(resolve, 100));
      const replies = values(selectors.replies);
      const users = values(selectors.users);
      const newlyRenderedUsers = users.slice(began.beforeUserCount);
      const exactUserTurn = newlyRenderedUsers.some(promptMatches)
        || Boolean(users.at(-1) && users.at(-1) !== began.beforeUserLast
          && promptMatches(users.at(-1)));
      const submitted = selectors.pairRepliesToUsers ? exactUserTurn : (replies.length > began.beforeCount
        || (replies.at(-1) && replies.at(-1) !== began.beforeLast)
        || users.length > began.beforeUserCount
        || (users.at(-1) && users.at(-1) !== began.beforeUserLast && users.at(-1).includes(prompt))
        || selectors.stop.some((selector) => [...document.querySelectorAll(selector)].some(visible)));
      if (submitted) return {...began, ok: true, needsTrustedEnter: false, error: ""};
    }
    return began;
  })()`;
}

function answerScript(provider, began) {
  return `(() => {
    const selectors = ${JSON.stringify({
      users: provider.users || [], replies: provider.replies, stop: provider.stop,
      errors: provider.errors || [], pairRepliesToUsers: Boolean(provider.pairRepliesToUsers),
    })};
    const began = ${JSON.stringify(began)};
    const visible = (one) => one && one.getClientRects().length && getComputedStyle(one).visibility !== "hidden";
    let replyNodes = [];
    for (const selector of selectors.replies) {
      replyNodes = [...document.querySelectorAll(selector)].filter(visible)
        .filter((one) => (one.innerText || one.textContent || "").trim());
      if (replyNodes.length) break;
    }
    const textOf = (one) => (one?.innerText || one?.textContent || "").trim();
    const values = replyNodes.map(textOf);
    let userNodes = [];
    for (const selector of selectors.users) {
      userNodes = [...document.querySelectorAll(selector)].filter(visible)
        .filter((one) => textOf(one));
      if (userNodes.length) break;
    }
    const users = userNodes.map(textOf);
    const stopping = selectors.stop.some((selector) => [...document.querySelectorAll(selector)].some(visible));
    let errors = [];
    for (const selector of selectors.errors) {
      errors = [...document.querySelectorAll(selector)].filter(visible)
        .map((one) => (one.getAttribute("aria-description") || one.innerText || one.textContent || "").trim())
        .filter(Boolean);
      if (errors.length) break;
    }
    const latestError = errors.at(-1) || "";
    const error = latestError && latestError !== began.beforeError ? latestError : "";
    const added = values.length > began.beforeCount ? values.slice(began.beforeCount) : [];
    const latest = values.at(-1) || "";
    const answer = added.at(-1) || (latest && latest !== began.beforeLast ? latest : "");
    let promptIndex = -1;
    let promptNode = null;
    if (began.submittedPrompt) {
      // Provider editors reflow long multi-line prompts when rendering the
      // user's bubble. Comparing innerText with the byte-for-byte submitted
      // prompt therefore fails for real Nexus board context, even though the
      // new user turn and its answer are both present. Normalise whitespace,
      // then fall back to the new-turn boundary captured before Send.
      const normal = (value) => String(value || "").replace(/\\s+/g, " ").trim();
      const submitted = normal(began.submittedPrompt);
      const marker = normal(began.submittedMarker);
      const promptMatches = (value) => {
        const rendered = normal(value);
        if (!rendered || !submitted) return false;
        // Every real submission carries a fresh transport marker. Provider
        // SPAs may unmount all turns while scrolling/loading and later remount
        // an old user/reply pair. Counts and text changes cannot distinguish
        // that old pair from a new turn, but the fresh marker can.
        if (marker) return rendered.includes(marker);
        if (rendered.includes(submitted)) return true;
        const head = submitted.slice(0, Math.min(180, submitted.length));
        const tail = submitted.slice(Math.max(0, submitted.length - 180));
        return head.length >= 40 && tail.length >= 40
          && rendered.includes(head) && rendered.includes(tail);
      };
      users.forEach((value, index) => {
        const isNewIndex = index >= began.beforeUserCount;
        const replacedLast = index === users.length - 1
          && value !== began.beforeUserLast;
        if ((isNewIndex || replacedLast) && promptMatches(value)) {
          promptIndex = index;
          promptNode = userNodes[index];
        }
      });
    }
    const userAdvanced = selectors.pairRepliesToUsers ? promptIndex >= 0
      : users.length > began.beforeUserCount
        || Boolean(users.at(-1) && users.at(-1) !== began.beforeUserLast);
    const replyAdvanced = values.length > began.beforeCount
      || Boolean(latest && latest !== began.beforeLast);
    if (selectors.pairRepliesToUsers && promptNode
        && typeof promptNode.compareDocumentPosition === "function") {
      // Counts are not causal evidence in virtualised SPAs: mounting the new
      // user bubble can simultaneously remount an old nested reply wrapper.
      // A reply belongs to this transport turn only when its actual DOM node
      // follows the uniquely marked user node in document order.
      const followingReplies = replyNodes.filter(
        (one) => Boolean(promptNode.compareDocumentPosition(one) & 4));
      const pairedAnswer = textOf(followingReplies.at(-1));
      return {answer: pairedAnswer, changed: Boolean(pairedAnswer), stopping, error};
    }
    const paired = !selectors.pairRepliesToUsers
      || (promptIndex >= 0 && userAdvanced && replyAdvanced);
    const changed = Boolean(answer) && paired;
    return {answer, changed, stopping, error};
  })()`;
}

function retryScript(provider) {
  return `(() => {
    const selectors = ${JSON.stringify(provider.retry || [])};
    const visible = (one) => one && one.getClientRects().length
      && getComputedStyle(one).visibility !== "hidden";
    const found = selectors.flatMap((selector) => [...document.querySelectorAll(selector)]).find(visible);
    const button = found?.matches("button, [role='button']")
      ? found : found?.querySelector("button, [role='button']");
    if (!button || button.disabled) return false;
    button.click();
    return true;
  })()`;
}

function stopScript(provider) {
  return `(() => {
    const selectors = ${JSON.stringify(provider.stop)};
    const visible = (one) => one && one.getClientRects().length && getComputedStyle(one).visibility !== "hidden";
    const found = selectors.flatMap((selector) => [...document.querySelectorAll(selector)]).find(visible);
    const button = found?.matches("button, [role='button']")
      ? found : found?.querySelector("button, [role='button']");
    if (!button || button.disabled) return false;
    button.click();
    return true;
  })()`;
}

class WebChatManager {
  constructor(options) {
    this.electron = options.electron;
    this.owner = options.owner;
    this.readSettings = options.readSettings;
    this.writeSettings = options.writeSettings;
    this.shellPage = options.shellPage;
    this.shellPreload = options.shellPreload;
    this.connections = new Map();
    this.views = new Map();
    this.shells = new Map();
    this.activeEmbedded = "";
    this.queues = new Map();
    this.activeAsks = new Map();
    this.externalTransports = new Map();
    this.backgroundHosts = new Map();
    this.externalBrowserFactory = options.externalBrowserFactory || ((transportOptions) => (
      new ExternalBrowserTransport(transportOptions)
    ));
    this.answerDeadlineMs = Math.max(1000, Number(options.answerDeadlineMs) || 165000);
    this.stalledReplyMs = Math.max(0, Number(options.stalledReplyMs) || 30000);
    this.answerPollMs = Math.max(1, Number(options.answerPollMs) || 900);
    this.stoppingSettleMs = options.stoppingSettleMs == null
      ? null : Math.max(1, Number(options.stoppingSettleMs) || 1);
    this.trackedContents = new WeakMap();
    const saved = this.readSettings().webChats;
    for (const raw of Array.isArray(saved) ? saved : []) {
      if (!raw || !PROVIDERS[raw.provider] || !allowedProviderUrl(PROVIDERS[raw.provider], raw.url)) continue;
      const id = String(raw.id || "").toLowerCase();
      if (!/^[a-z0-9][a-z0-9-]{5,63}$/.test(id)) continue;
      const threads = {};
      if (raw.threads && typeof raw.threads === "object" && !Array.isArray(raw.threads)) {
        for (const [candidateKey, candidate] of Object.entries(raw.threads)) {
          const key = cleanConversationKey(candidateKey);
          if (!key || !candidate || !specificConversationUrl(
            PROVIDERS[raw.provider], candidate.url
          )) continue;
          threads[key] = {
            url: String(candidate.url),
            title: String(candidate.title || "").slice(0, 120),
          };
        }
      }
      this.connections.set(id, {
        id, provider: raw.provider,
        title: String(raw.title || PROVIDERS[raw.provider].label).slice(0, 120),
        url: raw.url, threads,
      });
    }
  }

  providers() { return Object.values(PROVIDERS).map(({id, label, home, externalBrowser}) => (
    {id, label, home, external: Boolean(externalBrowser)}
  )); }
  list() { return [...this.connections.values()].map(publicConnection); }

  save(selected = null) {
    this.writeSettings({
      ...this.readSettings(),
      webChats: [...this.connections.values()].map(savedConnection),
    });
    this.changed(selected);
  }

  changed(selected = null) {
    if (this.owner && !this.owner.isDestroyed()) {
      this.owner.webContents.send(
        "harness:webChatsChanged", this.list(), selected ? publicConnection(selected) : null);
    }
  }

  sessionFor(providerId) {
    const {session} = this.electron;
    const provider = PROVIDERS[providerId];
    const held = session.fromPartition(`persist:nexus-web-chat-${providerId}`);
    if (held.__nexusWebChatGuarded) return held;
    held.__nexusWebChatGuarded = true;
    if (provider?.browserLikeUserAgent && held.getUserAgent && held.setUserAgent) {
      held.setUserAgent(browserLikeUserAgent(held.getUserAgent()));
    }
    held.setPermissionCheckHandler?.((_contents, permission, requestingOrigin, details = {}) => (
      authStorageAccessIsTrusted(provider, permission, [
        requestingOrigin, details.requestingUrl, details.embeddingOrigin,
      ])
    ));
    held.setPermissionRequestHandler((_contents, permission, answer, details = {}) => answer(
      // Electron has reported this request with the hCaptcha origin in
      // requestingOrigin or embeddingOrigin, while other Chromium versions
      // put it in requestingUrl.  The check handler already considers all
      // three; applying a narrower rule here silently rejects the challenge
      // and leaves Claude's Continue button spinning forever.
      authStorageAccessIsTrusted(provider, permission, [
        details.requestingUrl, details.requestingOrigin, details.embeddingOrigin,
      ])
    ));
    held.on("will-download", (event) => event.preventDefault());
    return held;
  }

  externalTransportFor(providerId) {
    const provider = PROVIDERS[providerId];
    if (!provider?.externalBrowser) return null;
    let transport = this.externalTransports.get(providerId);
    if (!transport) {
      const userData = this.electron.app?.getPath?.("userData") || process.cwd();
      transport = this.externalBrowserFactory({
        provider,
        preferred: provider.externalBrowser.preferred,
        profilePath: path.join(userData, "external-web-chat", providerId),
      });
      this.externalTransports.set(providerId, transport);
    }
    return transport;
  }

  threadFor(one, conversationKey, preferExisting = false) {
    const key = cleanConversationKey(conversationKey);
    if (!key) return {key: "", url: one.url, title: one.title};
    one.threads ||= {};
    if (one.threads[key]) return {key, ...one.threads[key]};
    const provider = PROVIDERS[one.provider];
    const baseAlreadyOwned = Object.values(one.threads).some(
      (thread) => thread.url === one.url
    );
    if (preferExisting && !baseAlreadyOwned && specificConversationUrl(provider, one.url)) {
      one.threads[key] = {url: one.url, title: one.title};
      this.save();
      return {key, ...one.threads[key]};
    }
    return {key, url: provider.newChat, title: provider.label};
  }

  rememberConnectionPage(id, contents, conversationKey = "") {
    const one = this.connections.get(String(id));
    if (!one || !contents || contents.isDestroyed?.()) return false;
    const provider = PROVIDERS[one.provider];
    const url = String(contents.getURL?.() || "");
    if (!allowedProviderUrl(provider, url)) return false;
    const title = String(contents.getTitle?.() || "").trim().slice(0, 120);
    const key = cleanConversationKey(conversationKey);
    if (key) {
      // Generic compose pages are useful while this process is alive, but they
      // do not identify a provider conversation after restart. Persist a
      // binding only after the provider gives the thread its own URL.
      if (!specificConversationUrl(provider, url)) return false;
      one.threads ||= {};
      const before = one.threads[key];
      if (before?.url === url && (!title || before.title === title)) return false;
      one.threads[key] = {url, title: title || before?.title || provider.label};
      this.save();
      return true;
    }
    if (url === one.url && (!title || title === one.title)) return false;
    one.url = url;
    if (title) one.title = title;
    this.save();
    return true;
  }

  trackConnectionPage(id, contents, conversationKey = "") {
    id = String(id || "");
    const key = cleanConversationKey(conversationKey);
    const tracked = channelKey(id, key);
    if (!id || !contents || this.trackedContents.get(contents) === tracked) return;
    this.trackedContents.set(contents, tracked);
    if (typeof contents.on !== "function") return;
    const remember = () => {
      if (this.trackedContents.get(contents) === tracked) {
        this.rememberConnectionPage(id, contents, key);
      }
    };
    for (const event of ["did-navigate", "did-navigate-in-page", "page-title-updated"]) {
      contents.on(event, remember);
    }
  }

  showCreatedConversationInOpenShells(id, conversationKey, source, priorUrl) {
    const one = this.connections.get(String(id));
    if (!one) return;
    const key = cleanConversationKey(conversationKey);
    const target = key ? one.threads?.[key]?.url : one.url;
    if (!target || target === priorUrl) return;
    for (const held of this.shells.values()) {
      const contents = held.view?.webContents;
      if (held.connectionId !== String(id) || held.conversationKey !== key
          || !contents || contents === source
          || contents.isDestroyed() || contents.getURL() !== priorUrl) continue;
      contents.loadURL(target).catch(() => {});
    }
  }

  guard(contents, provider, heldSession) {
    contents.setWindowOpenHandler(({url}) => {
      if (!allowedProviderUrl(provider, url, true)) return {action: "deny"};
      return {action: "allow", overrideBrowserWindowOptions: {parent: this.owner || undefined, webPreferences: {
        session: heldSession, nodeIntegration: false, contextIsolation: true, sandbox: true,
      }}};
    });
    for (const moment of ["will-navigate", "will-redirect"]) {
      contents.on(moment, (event, url) => {
        if (!allowedProviderUrl(provider, url, true)) event.preventDefault();
      });
    }
  }

  makeRemoteView(providerId, initialUrl = "") {
    const {WebContentsView} = this.electron;
    const provider = PROVIDERS[providerId];
    const external = this.externalTransportFor(providerId);
    if (external) {
      return {
        external: true,
        webContents: external.createContents(initialUrl || provider.home),
        setBounds: () => {},
      };
    }
    const heldSession = this.sessionFor(providerId);
    const view = new WebContentsView({webPreferences: {
      session: heldSession, nodeIntegration: false, contextIsolation: true, sandbox: true,
      webSecurity: true, allowRunningInsecureContent: false,
      // Chromium 150 may mark an offscreen child view as occluded. Gemini then
      // keeps generating but virtualizes every message node, so Nexus cannot
      // observe the answer until a person opens the page. Keep provider relay
      // views render-active while they are parked outside the app viewport.
      backgroundThrottling: false,
    }});
    // A detached WebContentsView otherwise has a 0x0 layout viewport. Provider
    // pages can still submit from it, but every reply then fails the visible
    // element check after navigation. Bounds give background automation a DOM
    // layout without attaching or displaying the view.
    view.setBounds({x: 0, y: 0, width: 1200, height: 900});
    this.guard(view.webContents, provider, heldSession);
    return view;
  }

  parkBackgroundView(view) {
    if (!view || view.external) return;
    const {BrowserWindow} = this.electron;
    if (typeof BrowserWindow !== "function") {
      // Lightweight test doubles and older Electron builds retain the prior
      // layout-preserving fallback.
      if (!this.owner || this.owner.isDestroyed()) return;
      this.owner.contentView.addChildView(view);
      view.setBounds({x: -12000, y: -12000, width: 1200, height: 900});
      return;
    }
    let host = this.backgroundHosts.get(view);
    if (!host || host.isDestroyed()) {
      host = new BrowserWindow({
        show: false, skipTaskbar: true, frame: false,
        width: 1200, height: 900,
        webPreferences: {
          nodeIntegration: false, contextIsolation: true, sandbox: true,
          backgroundThrottling: false,
        },
      });
      this.backgroundHosts.set(view, host);
      host.on("closed", () => {
        if (this.backgroundHosts.get(view) === host) this.backgroundHosts.delete(view);
      });
    }
    if (this.owner && !this.owner.isDestroyed()) {
      try { this.owner.contentView.removeChildView(view); } catch (_error) {}
    }
    host.contentView.addChildView(view);
    view.setBounds({x: 0, y: 0, width: 1200, height: 900});
  }

  releaseBackgroundHost(view) {
    const host = this.backgroundHosts.get(view);
    if (!host) return;
    try { host.contentView.removeChildView(view); } catch (_error) {}
    this.backgroundHosts.delete(view);
    if (!host.isDestroyed()) host.close();
  }

  async waitForLoad(contents) {
    if (!contents.isLoading()) return;
    await new Promise((resolve, reject) => {
      let settled = false;
      const cleanup = () => {
        contents.removeListener("did-finish-load", finished);
        contents.removeListener("did-fail-load", failed);
      };
      const finished = () => {
        if (settled) return;
        settled = true;
        cleanup();
        resolve();
      };
      const failed = (_event, code, description, _url, isMainFrame) => {
        if (isMainFrame === false || settled) return;
        settled = true;
        cleanup();
        reject(new Error(`${description} (${code})`));
      };
      contents.on("did-finish-load", finished);
      contents.on("did-fail-load", failed);
      // Loading can finish between the first isLoading() check and attaching
      // these listeners. Recheck after both listeners are in place.
      if (!contents.isLoading()) finished();
    });
  }

  openSetup(providerId, connectionId = "", conversationKey = "", preferExisting = false) {
    const provider = PROVIDERS[providerId];
    if (!provider) throw new Error("That web-chat provider is not supported");
    const {BrowserWindow} = this.electron;
    const external = Boolean(provider.externalBrowser);
    const shell = new BrowserWindow({
      width: external ? 720 : 1120, height: external ? 220 : 820,
      minWidth: external ? 620 : 720, minHeight: external ? 180 : 520,
      parent: this.owner || undefined, title: `${provider.label} web chat`,
      backgroundColor: "#071922",
      webPreferences: {preload: this.shellPreload, contextIsolation: true, nodeIntegration: false, sandbox: true},
    });
    const key = cleanConversationKey(conversationKey);
    const connection = this.connections.get(connectionId);
    const thread = connection
      ? this.threadFor(connection, key, preferExisting)
      : {url: provider.home};
    const view = this.makeRemoteView(providerId, thread.url);
    if (!view.external) shell.contentView.addChildView(view);
    const resize = () => {
      const [width, height] = shell.getContentSize();
      view.setBounds({x: 0, y: 56, width, height: Math.max(1, height - 56)});
    };
    shell.on("resize", resize);
    shell.on("closed", () => {
      this.shells.delete(shell.id);
      if (!view.webContents.isDestroyed()) view.webContents.close();
    });
    this.shells.set(shell.id, {
      shell, view, providerId, connectionId, conversationKey: key,
      preferExisting: Boolean(preferExisting),
    });
    shell.loadURL(`${this.shellPage}?provider=${encodeURIComponent(provider.label)}&external=${external ? "1" : "0"}`);
    if (connection) this.trackConnectionPage(connection.id, view.webContents, key);
    if (!view.external) view.webContents.loadURL(thread.url);
    resize();
    return true;
  }

  shellFor(contents) {
    const {BrowserWindow} = this.electron;
    const shell = BrowserWindow.fromWebContents(contents);
    return shell ? this.shells.get(shell.id) : null;
  }

  startNew(contents) {
    const held = this.shellFor(contents);
    if (!held) return false;
    if (held.connectionId && held.conversationKey) {
      const one = this.connections.get(held.connectionId);
      if (one?.threads?.[held.conversationKey]) {
        delete one.threads[held.conversationKey];
        this.save();
      }
    }
    held.view.webContents.loadURL(PROVIDERS[held.providerId].newChat);
    return true;
  }

  async useCurrent(contents) {
    const held = this.shellFor(contents);
    if (!held) throw new Error("That web-chat window is no longer open");
    const provider = PROVIDERS[held.providerId];
    await this.waitForLoad(held.view.webContents);
    const url = held.view.webContents.getURL();
    if (!allowedProviderUrl(provider, url)) throw new Error("Open a chat on the provider site before adding it to Nexus");
    const readiness = await held.view.webContents.executeJavaScript(
      providerReadinessScript(provider), true);
    if (!readiness?.ready) throw new Error(
      readiness?.reason || `Finish setting up ${provider.label}, then open a chat.`);
    const title = (await held.view.webContents.getTitle()) || provider.label;
    const id = held.connectionId || `${provider.id}-${crypto.randomBytes(6).toString("hex")}`;
    const existing = this.connections.get(id);
    const selected = existing || {
      id, provider: provider.id, title: title.slice(0, 120), url, threads: {},
    };
    if (held.conversationKey) {
      selected.threads ||= {};
      if (specificConversationUrl(provider, url)) {
        selected.threads[held.conversationKey] = {url, title: title.slice(0, 120)};
      }
    } else {
      selected.title = title.slice(0, 120);
      selected.url = url;
    }
    this.connections.set(id, selected);
    held.connectionId = id;
    this.trackConnectionPage(id, held.view.webContents, held.conversationKey);
    // A deliberate press of "Use this chat in Nexus" is stronger than an
    // ordinary connection-list refresh.  Tell the board which chat was chosen
    // so it can create (or select) the matching agent box immediately.
    this.save(selected);
    return publicConnection(selected);
  }

  openHeadered(id, conversationKey = "", preferExisting = false) {
    const one = this.connections.get(String(id));
    if (!one) return false;
    this.hideEmbedded();
    return this.openSetup(one.provider, one.id, conversationKey, preferExisting);
  }

  viewFor(id, conversationKey = "", preferExisting = false) {
    const one = this.connections.get(id);
    if (!one) throw new Error("That web chat is no longer connected");
    const key = cleanConversationKey(conversationKey);
    const channel = channelKey(id, key);
    let view = this.views.get(channel);
    if (!view || view.webContents.isDestroyed()) {
      const thread = this.threadFor(one, key, preferExisting);
      view = this.makeRemoteView(one.provider, thread.url);
      this.views.set(channel, view);
      this.parkBackgroundView(view);
      this.trackConnectionPage(id, view.webContents, key);
      if (!view.external) view.webContents.loadURL(thread.url);
    }
    return view;
  }

  showEmbedded(id, conversationKey = "", preferExisting = false, bounds = {}) {
    if (!this.owner || this.owner.isDestroyed()) return false;
    this.hideEmbedded();
    const key = cleanConversationKey(conversationKey);
    const view = this.viewFor(String(id), key, preferExisting);
    if (view.external) {
      view.webContents.bringToFront().catch(() => {});
      return {external: true, provider: this.connections.get(String(id))?.provider || ""};
    }
    const host = this.backgroundHosts.get(view);
    if (host && !host.isDestroyed()) {
      try { host.contentView.removeChildView(view); } catch (_error) {}
    }
    this.owner.contentView.addChildView(view);
    this.activeEmbedded = channelKey(id, key);
    this.resizeEmbedded(id, key, bounds);
    return true;
  }

  resizeEmbedded(id, conversationKey = "", bounds = {}) {
    const channel = channelKey(id, conversationKey);
    if (channel !== this.activeEmbedded) return false;
    const view = this.views.get(channel);
    if (!view) return false;
    if (view.external) return true;
    const clean = {};
    for (const key of ["x", "y", "width", "height"]) clean[key] = Math.max(key === "width" || key === "height" ? 1 : 0, Math.round(Number(bounds?.[key]) || 0));
    view.setBounds(clean);
    return true;
  }

  hideEmbedded() {
    if (!this.activeEmbedded || !this.owner || this.owner.isDestroyed()) return false;
    const view = this.views.get(this.activeEmbedded);
    if (view) this.parkBackgroundView(view);
    this.activeEmbedded = "";
    return true;
  }

  remove(id) {
    id = String(id);
    if (!this.connections.has(id)) return false;
    if (this.activeEmbedded === id || this.activeEmbedded.startsWith(`${id}\n`)) {
      this.hideEmbedded();
    }
    for (const [channel, view] of [...this.views.entries()]) {
      if (channel !== id && !channel.startsWith(`${id}\n`)) continue;
      if (!view.webContents.isDestroyed()) {
        this.releaseBackgroundHost(view);
        if (!view.external && this.owner && !this.owner.isDestroyed()) {
          this.owner.contentView.removeChildView(view);
        }
        view.webContents.close();
      }
      this.views.delete(channel);
    }
    this.connections.delete(id);
    this.save();
    return true;
  }

  resetThread(id, conversationKey) {
    id = String(id || "");
    const key = cleanConversationKey(conversationKey);
    if (!key || !this.connections.has(id)) return false;
    const channel = channelKey(id, key);
    if (this.activeEmbedded === channel) this.hideEmbedded();
    const view = this.views.get(channel);
    if (view && !view.webContents.isDestroyed()) {
      this.releaseBackgroundHost(view);
      if (!view.external && this.owner && !this.owner.isDestroyed()) {
        this.owner.contentView.removeChildView(view);
      }
      view.webContents.close();
    }
    this.views.delete(channel);
    const one = this.connections.get(id);
    const changed = Boolean(one.threads?.[key]);
    if (changed) {
      delete one.threads[key];
      this.save();
    }
    return true;
  }

  ask(
    id, prompt, attachments = [], conversationKey = "", preferExisting = false
  ) {
    id = String(id || "");
    const key = cleanConversationKey(conversationKey);
    const channel = channelKey(id, key);
    const before = this.queues.get(channel) || Promise.resolve();
    const mine = before.catch(() => {}).then(() => this.askNow(
      id, String(prompt || ""), Array.isArray(attachments) ? attachments : [],
      key, preferExisting));
    this.queues.set(channel, mine);
    return mine.finally(() => {
      if (this.queues.get(channel) === mine) this.queues.delete(channel);
    });
  }

  async stop(id, conversationKey = "") {
    id = String(id || "");
    const channel = channelKey(id, conversationKey);
    const active = this.activeAsks.get(channel);
    if (!active) return false;
    active.cancelled = true;
    try {
      const one = this.connections.get(id);
      const view = this.views.get(channel);
      if (one && view && !view.webContents.isDestroyed()) {
        await view.webContents.executeJavaScript(stopScript(PROVIDERS[one.provider]), true);
      }
    } catch (_error) {
      // The local cancellation is authoritative. The provider's stop control
      // is best-effort because the page may be navigating as Stop is pressed.
    }
    return true;
  }

  async attachFiles(contents, provider, attachments) {
    const files = attachments.map((one) => String(one.path || "")).filter(Boolean);
    if (!files.length) return;
    const click = `(() => {
      const selectors = ${JSON.stringify(provider.attach || [])};
      const visible = (one) => one && one.getClientRects().length;
      let input = document.querySelector("input[type=file]");
      if (input) return true;
      const button = selectors
        .flatMap((selector) => [...document.querySelectorAll(selector)]).find(visible);
      if (button) button.click();
      return Boolean(button);
    })()`;
    await contents.executeJavaScript(click, true);
    if (typeof contents.setFiles === "function") {
      await contents.setFiles(files);
      await new Promise((resolve) => setTimeout(resolve, 500));
      return;
    }
    let nodeId = 0;
    const attachedHere = !contents.debugger.isAttached();
    if (attachedHere) contents.debugger.attach("1.3");
    try {
      for (let tries = 0; tries < 20 && !nodeId; tries += 1) {
        const root = await contents.debugger.sendCommand("DOM.getDocument", {depth: 1});
        const found = await contents.debugger.sendCommand("DOM.querySelectorAll", {
          nodeId: root.root.nodeId, selector: "input[type=file]",
        });
        nodeId = found.nodeIds.at(-1) || 0;
        if (!nodeId) await new Promise((resolve) => setTimeout(resolve, 150));
      }
      if (!nodeId) throw new Error(`Nexus could not find ${provider.label}'s file attachment control`);
      await contents.debugger.sendCommand("DOM.setFileInputFiles", {nodeId, files});
      await new Promise((resolve) => setTimeout(resolve, 500));
    } finally {
      if (attachedHere && contents.debugger.isAttached()) contents.debugger.detach();
    }
  }

  async askNow(
    id, prompt, attachments = [], conversationKey = "", preferExisting = false
  ) {
    const one = this.connections.get(id);
    if (!one) throw new Error("That web chat is no longer connected");
    const key = cleanConversationKey(conversationKey);
    const channel = channelKey(id, key);
    const active = {cancelled: false};
    this.activeAsks.set(channel, active);
    const stopped = () => {
      if (active.cancelled) throw new Error("Stopped by you.");
    };
    try {
      const view = this.viewFor(id, key, preferExisting);
      await this.waitForLoad(view.webContents);
      stopped();
      const provider = PROVIDERS[one.provider];
      const priorUrl = String(view.webContents.getURL?.() || (
        this.threadFor(one, key, preferExisting).url
      ));
      await this.attachFiles(view.webContents, provider, attachments);
      stopped();
      const nextSubmission = () => {
        const marker = `NEXUS TRANSPORT TURN ${crypto.randomUUID()}`;
        return {marker, prompt: `[${marker}]\n\n${prompt}`};
      };
      const submitTurn = async (submission) => {
        // ChatGPT's ProseMirror/React composer ignores programmatic input while
        // its WebContentsView is parked offscreen unless that web contents owns
        // focus. Focus it only for the bounded fill/send handshake, then return
        // focus to the Nexus board before waiting for the remote answer.
        view.webContents.focus?.();
        try {
          let began = await view.webContents.executeJavaScript(
            automationScript(provider, submission.prompt, submission.marker), true);
          stopped();
          if (!began?.ok && began?.needsTrustedEnter
              && (typeof view.webContents.sendInputEvent === "function"
                  || typeof view.webContents.pressEnter === "function")) {
            if (typeof view.webContents.pressEnter === "function") {
              await view.webContents.pressEnter();
            } else {
              view.webContents.sendInputEvent({type: "keyDown", keyCode: "ENTER"});
              view.webContents.sendInputEvent({type: "keyUp", keyCode: "ENTER"});
            }
            began = await view.webContents.executeJavaScript(
              submissionScript(provider, submission.prompt, began), true);
            stopped();
          }
          return began;
        } finally {
          if (this.owner && !this.owner.isDestroyed?.()) this.owner.webContents?.focus?.();
        }
      };
      let submission = nextSubmission();
      let began = await submitTurn(submission);
      if (!began?.ok) throw new Error(began?.error || "The provider web chat could not be sent a message");
      const started = Date.now();
      let attemptStarted = started;
      let stallRetries = 0;
      let retriedVisibleError = false;
      let replyChanged = false;
      let stable = 0;
      let previous = "";
      while (Date.now() - started < this.answerDeadlineMs) {
        await new Promise((resolve) => setTimeout(resolve, this.answerPollMs));
        stopped();
        const state = await view.webContents.executeJavaScript(answerScript(provider, began), true);
        stopped();
        if (state?.error) {
          if (provider.retryVisibleError && !retriedVisibleError) {
            const retried = await view.webContents.executeJavaScript(retryScript(provider), true);
            stopped();
            if (retried) {
              retriedVisibleError = true;
              attemptStarted = Date.now();
              stable = 0;
              previous = "";
              continue;
            }
          }
          throw new Error(`${provider.label} reported: ${state.error}`);
        }
        replyChanged ||= Boolean(state?.changed && state.answer);
        if (
          provider.retryStalledReply && !attachments.length
          && stallRetries < (provider.maxStallRetries || 1)
          && !replyChanged && state?.stopping
          && Date.now() - attemptStarted >= this.stalledReplyMs
        ) {
          const stoppedRemote = await view.webContents.executeJavaScript(stopScript(provider), true);
          stopped();
          if (stoppedRemote) {
            stallRetries += 1;
            stable = 0;
            previous = "";
            await new Promise((resolve) => setTimeout(resolve, Math.min(1200, this.answerPollMs)));
            stopped();
            submission = nextSubmission();
            began = await submitTurn(submission);
            stopped();
            if (!began?.ok) throw new Error(
              began?.error || `${provider.label} could not retry its stalled reply`);
            attemptStarted = Date.now();
            continue;
          }
        }
        if (!state?.changed || !state.answer) { stable = 0; continue; }
        if (state.answer === previous) stable += 1; else stable = 0;
        previous = state.answer;
        if (state.stopping) {
          const settleMs = this.stoppingSettleMs ?? provider.settleWhileStoppingMs ?? 0;
          if (!settleMs || stable * this.answerPollMs < settleMs) continue;
          await view.webContents.executeJavaScript(stopScript(provider), true);
          await new Promise((resolve) => setTimeout(resolve, Math.min(700, this.answerPollMs)));
          stopped();
          const committed = await view.webContents.executeJavaScript(answerScript(provider, began), true);
          const answer = committed?.changed && committed.answer ? committed.answer : state.answer;
          this.rememberConnectionPage(id, view.webContents, key);
          this.showCreatedConversationInOpenShells(
            id, key, view.webContents, priorUrl);
          return {answer, milliseconds: Date.now() - started, model: `${provider.label} web chat`};
        }
        if (stable >= 2) {
          this.rememberConnectionPage(id, view.webContents, key);
          this.showCreatedConversationInOpenShells(
            id, key, view.webContents, priorUrl);
          return {answer: state.answer, milliseconds: Date.now() - started, model: `${provider.label} web chat`};
        }
      }
      // Do not leave a provider generation running after the local broker has
      // stopped waiting. Stopping also gives providers one last chance to
      // commit a reply that was already visible but still marked as streaming.
      try {
        await view.webContents.executeJavaScript(stopScript(provider), true);
        await new Promise((resolve) => setTimeout(resolve, Math.min(700, this.answerPollMs)));
        stopped();
        const finalState = await view.webContents.executeJavaScript(answerScript(provider, began), true);
        if (finalState?.changed && finalState.answer) {
          this.rememberConnectionPage(id, view.webContents, key);
          this.showCreatedConversationInOpenShells(
            id, key, view.webContents, priorUrl);
          return {answer: finalState.answer, milliseconds: Date.now() - started, model: `${provider.label} web chat`};
        }
      } catch (error) {
        if (active.cancelled) throw error;
      }
      throw new Error(
        `${provider.label} did not finish a visible reply before the Nexus chat wait ended`);
    } finally {
      if (this.activeAsks.get(channel) === active) this.activeAsks.delete(channel);
    }
  }

  close() {
    this.hideEmbedded();
    for (const held of this.shells.values()) if (!held.shell.isDestroyed()) held.shell.close();
    for (const view of this.views.values()) if (!view.webContents.isDestroyed()) view.webContents.close();
    this.views.clear();
    for (const host of this.backgroundHosts.values()) if (!host.isDestroyed()) host.close();
    this.backgroundHosts.clear();
    for (const transport of this.externalTransports.values()) transport.close().catch(() => {});
    this.externalTransports.clear();
  }
}

module.exports = {
  PROVIDERS, WebChatManager, allowedProviderUrl, providerReadinessScript,
  automationScript, submissionScript, answerScript, retryScript, stopScript,
  browserLikeUserAgent, authStorageAccessIsTrusted,
};
