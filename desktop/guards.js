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


function onlyOnce(said) {
  // The same sentence twelve times over is not more information than once. The
  // harness prints one line per attempt and they were all pasted together, so
  // the page opened with a paragraph of the same words repeating.
  const seen = [];
  for (const one of String(said || "").split(/(?<=\.)\s+|\n/)) {
    const tidy = one.trim();
    if (tidy && !seen.includes(tidy)) seen.push(tidy);
  }
  return seen.join(" ") || String(said || "");
}

function whyItReallyIs(said) {
  // The app carries its own copy of the harness - that is how it runs with
  // nothing installed - and that copy is only as new as the last time somebody
  // built the app. Settings written by a newer harness can name things this
  // copy has never heard of.
  const held = String(said || "");
  if (/must name a supported provider|is not a kind|Unknown config key/i.test(held)) {
    return (
      "This app carries its own copy of the harness, and that copy looks older "
      + "than your settings: the settings name something it has never heard of. "
      + "Nothing is wrong with Python or with the folder. Install the newest "
      + "version of this app, or open the project with "
      + "python scripts/harness.py ui, which uses the code in the folder itself."
    );
  }
  if (/has not been told to trust/i.test(held)) {
    return (
      "This project has a settings file, and a settings file can name commands "
      + "to run - so nothing reads one until you say the file is yours. That is "
      + "a deliberate stop, not a fault. Run the installer again and say yes "
      + "when it asks, or run python scripts/harness.py trust in the folder."
    );
  }
  return "";
}

module.exports = { attachGuards, onlyOnce, whyItReallyIs };
