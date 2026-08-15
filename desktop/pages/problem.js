"use strict";

const query = new URLSearchParams(location.search);
const title = query.get("title");
const detail = query.get("detail");
const log = query.get("log");

if (title) document.getElementById("title").textContent = title;
document.getElementById("detail").textContent = detail || "The harness did not start.";
if (log) {
  document.getElementById("logTitle").hidden = false;
  const box = document.getElementById("log");
  box.hidden = false;
  box.textContent = log;
}
document.getElementById("retry").addEventListener("click", () => window.harnessDesktop.retry());
document.getElementById("choose").addEventListener("click", () => window.harnessDesktop.chooseProject());
