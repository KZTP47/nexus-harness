"use strict";

const project = new URLSearchParams(location.search).get("project") || "";
if (project) {
  document.getElementById("detail").textContent = `Opening ${project}.`;
}
document.getElementById("choose").addEventListener("click", () => window.harnessDesktop.chooseProject());
