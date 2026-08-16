"use strict";

const status = document.getElementById("status");

document.getElementById("choose").addEventListener("click", async () => {
  status.textContent = "Waiting for you to pick a folder.";
  const chosen = await window.harnessDesktop.chooseProject();
  status.textContent = chosen ? "" : "No folder was chosen. Press the button when you are ready.";
});
document.getElementById("help").addEventListener("click", () => window.harnessDesktop.showHelp());
