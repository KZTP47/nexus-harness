"use strict";

// Where the window is allowed to go, and what it may hand to the rest of the
// machine. Kept apart from the window itself so the rules can be tried on
// their own, without opening anything.

const { isLoopbackUrl, isWebAddress } = require("./server");

function attachGuards(contents, options = {}) {
  const openExternally = options.openExternally || (() => {});
  const allowed = options.allowedTarget;
  if (typeof allowed !== "function") {
    // Without this the window would refuse to go anywhere at all, including
    // its own pages, and the app would look broken with nothing to explain it.
    throw new Error("attachGuards needs allowedTarget, or the window can go nowhere");
  }
  // The window only ever shows this machine. A web address opens in the
  // user's own browser, where they can see it before they trust it. Anything
  // that is not a web address is dropped: handing it to the system would let
  // whatever the window is showing start a program or open a file share.
  contents.setWindowOpenHandler(({ url }) => {
    if (!isLoopbackUrl(url) && isWebAddress(url)) openExternally(url);
    return { action: "deny" };
  });
  // A page can be told to go somewhere, and it can also answer "go somewhere
  // else instead". Both ways in need the same rule.
  for (const moment of ["will-navigate", "will-redirect"]) {
    contents.on(moment, (event, url) => {
      if (!allowed(url)) event.preventDefault();
    });
  }
  return contents;
}

module.exports = { attachGuards };
