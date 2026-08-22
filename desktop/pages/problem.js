"use strict";

const query = new URLSearchParams(location.search);
const title = query.get("title");
const detail = query.get("detail");
const log = query.get("log");
const because = query.get("because");

if (title) document.getElementById("title").textContent = title;
document.getElementById("detail").textContent = detail || "The harness did not start.";
// When the app can tell what really happened, that is what somebody reads, and
// the three guesses are put away. Three wrong guesses send somebody looking in
// three wrong places - Python was installed, the folder was right, and nothing
// was missing from the download.
if (because) {
  const said = document.getElementById("because");
  said.textContent = because;
  said.hidden = false;
  document.getElementById("guesses").hidden = true;
}
if (log) {
  document.getElementById("logTitle").hidden = false;
  const box = document.getElementById("log");
  box.hidden = false;
  box.textContent = log;
}
document.getElementById("retry").addEventListener("click", () => window.harnessDesktop.retry());
document.getElementById("choose").addEventListener("click", () => window.harnessDesktop.chooseProject());
