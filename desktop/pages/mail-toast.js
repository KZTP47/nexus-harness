"use strict";

// The corner pop-up. It shows at most three cards, newest nearest the corner,
// each for a few seconds (paused while the pointer rests on it). Everything is
// written with textContent: an email subject is text, never markup.

(function () {
  const bridge = window.nexusToast;
  const stack = document.getElementById("stack");
  const DURATION_MS = 7000;
  const MOST_AT_ONCE = 3;
  if (!bridge || !stack) return;

  // A timer, not requestAnimationFrame: the window is still hidden when the
  // first card arrives, and a hidden window never gets an animation frame, so
  // it would wait for ever to be shown.
  function reportHeight() {
    setTimeout(() => {
      const cards = stack.querySelectorAll(".mail-toast:not(.leaving)").length;
      bridge.resize(cards ? Math.ceil(stack.getBoundingClientRect().height) : 0);
    }, 0);
  }

  function leave(card) {
    if (card.classList.contains("leaving")) return;
    clearTimeout(card.nexusTimer);
    card.classList.add("leaving");
    const gone = () => { card.remove(); reportHeight(); };
    card.addEventListener("animationend", gone, { once: true });
    setTimeout(gone, 400);
  }

  function countDown(card, remaining) {
    card.nexusStarted = Date.now();
    card.nexusRemaining = remaining;
    card.nexusTimer = setTimeout(() => leave(card), remaining);
  }

  function line(text, className) {
    const node = document.createElement("span");
    node.className = className;
    node.textContent = text;
    return node;
  }

  function build(notice) {
    const card = document.createElement("div");
    card.className = "mail-toast";
    card.setAttribute("role", "button");
    card.dataset.id = notice.id;
    card.style.setProperty("--mail-toast-duration", `${DURATION_MS}ms`);
    card.title = "Open this email in Nexus Harness";
    const avatar = line(notice.avatar || "@", "mail-toast-avatar");
    avatar.setAttribute("aria-hidden", "true");
    const text = document.createElement("span");
    text.className = "mail-toast-text";
    text.append(line(notice.title, "mail-toast-title"));
    if (notice.action) text.append(line(notice.action, "mail-toast-action mail-toast-working"));
    if (notice.detail) text.append(line(notice.detail, "mail-toast-detail"));
    const close = document.createElement("button");
    close.type = "button";
    close.className = "mail-toast-close";
    close.textContent = "×";
    close.setAttribute("aria-label", "Dismiss");
    close.addEventListener("click", (event) => { event.stopPropagation(); leave(card); });
    const progress = document.createElement("span");
    progress.className = "mail-toast-progress";
    card.append(avatar, text, close, progress);
    card.addEventListener("click", () => { bridge.activate(notice.id); leave(card); });
    card.addEventListener("mouseenter", () => {
      clearTimeout(card.nexusTimer);
      card.nexusRemaining = Math.max(1200, card.nexusRemaining - (Date.now() - card.nexusStarted));
    });
    card.addEventListener("mouseleave", () => countDown(card, card.nexusRemaining));
    return card;
  }

  bridge.onShow((notice) => {
    if (!notice || !notice.id || !notice.title) return;
    if (stack.querySelector(`[data-id="${CSS.escape(notice.id)}"]`)) return;
    const card = build(notice);
    stack.append(card);
    const live = [...stack.querySelectorAll(".mail-toast:not(.leaving)")];
    for (const old of live.slice(0, Math.max(0, live.length - MOST_AT_ONCE))) leave(old);
    countDown(card, DURATION_MS);
    reportHeight();
  });
})();
